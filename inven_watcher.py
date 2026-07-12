#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inven_watcher.py — 로스트아크 인벤 벨가르딘 공략 글 감시기

동작 흐름:
  1. 자유게시판 / 팁과 노하우 게시판 목록 페이지를 긁는다
  2. 이미 본 글(state.json)은 건너뛰고, 신규 글만 키워드 필터
  3. 키워드 통과 글의 본문을 가져와 Claude API로 스코어링 + 3줄 요약
  4. 점수가 임계치 이상이면 디스코드 웹훅으로 전송

환경변수(필수):
  ANTHROPIC_API_KEY   : Claude API 키
  DISCORD_WEBHOOK_URL : 디스코드 채널 웹훅 URL

주의:
  - 인벤 콘텐츠는 저작권 보호 대상. 본문 전체를 퍼가지 말고
    "제목 + 짧은 요약 + 원문 링크"만 전송한다. (이 스크립트의 기본 동작)
  - 목록 페이지는 주기적으로, 본문은 키워드 통과 글만 개별 요청한다.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ──────────────────────────────────────────────
# 주제 프리셋 — ACTIVE_PRESET 한 줄만 바꾸면 감시 주제가 전환된다
#   리허설(~8/4): "리허설_환영술사"  →  본작전(8/5~): "본작전_벨가르딘"
# ──────────────────────────────────────────────
PRESETS = {
    "리허설_환영술사": {
        "keywords": ["환영술사"],
        "topic": (
            "신규 클래스 '환영술사' 관련 유용 정보 "
            "(스킬·트라이포드, 각인·세팅, 딜사이클, 육성 팁, 성능 분석)"
        ),
    },
    "본작전_벨가르딘": {
        "keywords": ["벨가르딘"],
        "topic": (
            "'벨가르딘' 레이드 공략 정보 "
            "(기믹 파훼법, 관문별 팁, 추천 세팅, 트라이 중 검증된 공략)"
        ),
    },
}
ACTIVE_PRESET = "리허설_환영술사"

# ──────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────
CONFIG = {
    # 감시 대상 게시판: {게시판ID: 표시이름}
    # 10추/30추 모음은 자유게시판의 필터 뷰이므로 따로 긁지 않는다(중복).
    # 대신 목록에서 파싱한 추천수로 "🔥10추+" 태그를 붙인다.
    "boards": {
        "6271": "자유게시판",
        "4821": "팁과 노하우",
    },
    "list_url": "https://www.inven.co.kr/board/lostark/{board_id}",
    # keywords / topic 은 아래에서 ACTIVE_PRESET 값으로 채워진다
    # 게시판별 최소 점수 (Claude 0~10점). 팁게는 신호가 강하니 낮게.
    "min_score": {"6271": 6, "4821": 4},
    # 추천수가 이 값 이상이면 점수 미달이어도 전송 (집단지성 신호)
    "recommend_override": 10,
    # 본문을 Claude에 넘길 때 최대 길이(자)
    "body_max_chars": 4000,
    # 요청 간 대기(초) — 인벤 서버 예의
    "request_delay": 1.5,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    # Claude 설정
    "claude_model": "claude-haiku-4-5-20251001",
    "claude_max_tokens": 600,
    # 상태 파일
    "state_file": os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
    # 첫 실행 시 기존 글을 전부 알림으로 쏟아내지 않도록,
    # 첫 실행에서는 "본 글로 기록만" 하고 알림은 보내지 않는다.
    "first_run_silent": True,
}
# 활성 프리셋 병합
CONFIG.update(PRESETS[ACTIVE_PRESET])

KST = timezone(timedelta(hours=9))
POST_URL_RE = re.compile(r"/board/lostark/(\d+)/(\d+)")


# ──────────────────────────────────────────────
# 상태 관리
# ──────────────────────────────────────────────
def load_state(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"seen": {}, "initialized": False}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ──────────────────────────────────────────────
# 인벤 파싱
# ──────────────────────────────────────────────
def http_get(url, session):
    r = session.get(url, timeout=15)
    r.raise_for_status()
    # 인코딩 방어: 헤더가 부정확하면 추정 인코딩 사용
    if not r.encoding or r.encoding.lower() in ("iso-8859-1",):
        r.encoding = r.apparent_encoding
    return r.text


def parse_board_list(html, board_id):
    """
    목록 페이지에서 글 목록 추출.
    CSS 클래스에 의존하지 않고, 글 URL 패턴(/board/lostark/{board}/{post})으로
    앵커를 전부 수집하는 방어적 방식. 인벤 마크업이 바뀌어도 잘 버틴다.
    반환: [{post_id, title, url, recommend}, ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    posts = {}
    for a in soup.find_all("a", href=True):
        m = POST_URL_RE.search(a["href"])
        if not m or m.group(1) != board_id:
            continue
        post_id = m.group(2)
        title = a.get_text(" ", strip=True)
        # 제목 끝의 댓글수 표기 "[12]" / "[댓글 12]" 노이즈 제거
        title = re.sub(r"\s*\[(?:댓글\s*)?\d+\]\s*$", "", title)
        if not title or len(title) < 2:
            continue

        # 추천수 추출 시도: 같은 행(tr) 안에서 추천 관련 셀을 찾는다.
        # 실패해도 치명적이지 않으므로 None 허용.
        recommend = None
        tr = a.find_parent("tr")
        if tr:
            for td in tr.find_all("td"):
                cls = " ".join(td.get("class") or [])
                if any(k in cls for k in ("reco", "recommend", "sympathy")):
                    txt = td.get_text(strip=True)
                    if txt.isdigit():
                        recommend = int(txt)
                        break

        # 같은 글에 여러 앵커가 걸릴 수 있음 → 필드별로 병합
        if post_id in posts:
            exist = posts[post_id]
            if len(title) > len(exist["title"]):
                exist["title"] = title
            if exist["recommend"] is None and recommend is not None:
                exist["recommend"] = recommend
            continue

        posts[post_id] = {
            "post_id": post_id,
            "board": board_id,
            "title": title,
            "url": f"https://www.inven.co.kr/board/lostark/{board_id}/{post_id}",
            "recommend": recommend,
        }
    return list(posts.values())


def fetch_post_body(url, session):
    """본문 텍스트 추출. 알려진 컨테이너 후보를 순서대로 시도."""
    html = http_get(url, session)
    soup = BeautifulSoup(html, "html.parser")
    candidates = [
        {"id": "powerbbsContent"},
        {"class_": "articleContent"},
        {"id": "BoardContent"},
        {"class_": "contentBody"},
    ]
    for sel in candidates:
        node = soup.find(attrs={"id": sel["id"]}) if "id" in sel else soup.find(class_=sel["class_"])
        if node:
            text = node.get_text("\n", strip=True)
            if len(text) > 30:
                return text
    # 최후 수단: 페이지 전체 텍스트에서 네비게이션 잡음 이후 부분
    return soup.get_text("\n", strip=True)


# ──────────────────────────────────────────────
# Claude 평가
# ──────────────────────────────────────────────
def claude_evaluate(title, body, api_key, cfg):
    """
    반환: {"score": int 0~10, "category": str, "summary": [str, str, str]}
    실패 시 None (호출부에서 '평가 불가' 처리).
    """
    body = body[: cfg["body_max_chars"]]
    system = (
        "당신은 로스트아크 공대의 정보 큐레이터입니다. "
        f"인벤 게시글이 다음 주제에 실질적으로 도움이 되는 정보인지 평가하세요.\n"
        f"주제: {cfg['topic']}\n"
        "점수 기준: 9~10 구체적이고 검증된 핵심 정보, 7~8 유용한 팁·분석, "
        "5~6 부분적으로 유용, 3~4 후기·감상 위주, 0~2 뻘글·어그로·잡담·주제 무관.\n"
        "반드시 아래 JSON만 출력하세요. 다른 텍스트, 마크다운 펜스 금지.\n"
        '{"score": <0-10 정수>, "category": "<기믹|공략|세팅|정보|후기|뻘글|기타>", '
        '"summary": ["<핵심1>", "<핵심2>", "<핵심3>"]}'
    )
    payload = {
        "model": cfg["claude_model"],
        "max_tokens": cfg["claude_max_tokens"],
        "system": system,
        "messages": [
            {"role": "user", "content": f"제목: {title}\n\n본문:\n{body}"}
        ],
    }
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"```(?:json)?|```", "", text).strip()
        result = json.loads(text)
        result["score"] = int(result.get("score", 0))
        result["summary"] = [str(s) for s in result.get("summary", [])][:3]
        result["category"] = str(result.get("category", "기타"))
        return result
    except Exception as e:  # noqa: BLE001 — 평가 실패는 치명적이지 않음
        print(f"  [경고] Claude 평가 실패: {e}", file=sys.stderr)
        return None


# ──────────────────────────────────────────────
# 디스코드 전송
# ──────────────────────────────────────────────
def send_discord(webhook_url, post, board_name, evaluation, cfg):
    reco = post.get("recommend")
    reco_tag = ""
    if isinstance(reco, int):
        if reco >= 30:
            reco_tag = " 🔥30추+"
        elif reco >= 10:
            reco_tag = " 🔥10추+"

    if evaluation:
        score = evaluation["score"]
        desc_lines = [f"• {s}" for s in evaluation["summary"]]
        footer = f"{board_name} · [{evaluation['category']}] 정보성 {score}/10"
        color = 0x2ECC71 if score >= 8 else 0xF1C40F if score >= 6 else 0x95A5A6
    else:
        desc_lines = ["(AI 요약 실패 — 원문 링크로 직접 확인)"]
        footer = f"{board_name} · 평가 불가"
        color = 0x95A5A6

    embed = {
        "title": (post["title"] + reco_tag)[:250],
        "url": post["url"],
        "description": "\n".join(desc_lines)[:2000],
        "color": color,
        "footer": {"text": footer},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    if r.status_code == 429:  # 디스코드 rate limit
        wait = r.json().get("retry_after", 2)
        time.sleep(float(wait) + 0.5)
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    elif r.status_code >= 400:
        print(f"  [경고] 디스코드 전송 실패 {r.status_code}: {r.text[:200]}", file=sys.stderr)


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        print("[오류] DISCORD_WEBHOOK_URL 환경변수가 없습니다.", file=sys.stderr)
        sys.exit(1)
    if not api_key:
        print("[경고] ANTHROPIC_API_KEY 없음 → AI 요약 없이 제목+링크만 전송합니다.", file=sys.stderr)

    cfg = CONFIG
    state = load_state(cfg["state_file"])
    first_run = not state.get("initialized", False)
    session = requests.Session()
    session.headers.update({"User-Agent": cfg["user_agent"]})

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    print(f"=== 인벤 감시 실행 {now} KST (first_run={first_run}) ===")

    new_count, sent_count = 0, 0
    for board_id, board_name in cfg["boards"].items():
        url = cfg["list_url"].format(board_id=board_id)
        try:
            html = http_get(url, session)
        except Exception as e:  # noqa: BLE001
            print(f"[오류] {board_name} 목록 요청 실패: {e}", file=sys.stderr)
            continue

        posts = parse_board_list(html, board_id)
        if not posts:
            print(f"[경고] {board_name}: 파싱 결과 0건 — 마크업 변경/차단 여부 확인 필요", file=sys.stderr)
            # 디버그용 원본 저장 (덮어쓰기)
            debug_path = os.path.join(os.path.dirname(cfg["state_file"]), f"debug_{board_id}.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            continue

        print(f"{board_name}: 목록 {len(posts)}건")
        for post in posts:
            key = f"{board_id}:{post['post_id']}"
            if key in state["seen"]:
                continue
            state["seen"][key] = {"t": now, "title": post["title"][:80]}
            new_count += 1

            # 첫 실행: 기록만 하고 알림 폭탄 방지
            if first_run and cfg["first_run_silent"]:
                continue
            # 키워드 필터
            if not any(k in post["title"] for k in cfg["keywords"]):
                continue

            print(f"  → 키워드 매치: {post['title'][:60]}")
            time.sleep(cfg["request_delay"])

            evaluation = None
            if api_key:
                try:
                    body = fetch_post_body(post["url"], session)
                except Exception as e:  # noqa: BLE001
                    print(f"  [경고] 본문 요청 실패: {e}", file=sys.stderr)
                    body = ""
                if body:
                    evaluation = claude_evaluate(post["title"], body, api_key, cfg)

            # 전송 판정
            min_score = cfg["min_score"].get(board_id, 6)
            reco = post.get("recommend") or 0
            should_send = (
                evaluation is None  # 평가 불가 → 사람이 직접 판단하도록 일단 전송
                or evaluation["score"] >= min_score
                or reco >= cfg["recommend_override"]
            )
            if should_send:
                send_discord(webhook, post, board_name, evaluation, cfg)
                sent_count += 1
                time.sleep(1)
            else:
                sc = evaluation["score"] if evaluation else "-"
                print(f"  → 스킵 (점수 {sc} < {min_score}, 추천 {reco})")

    # seen 무한 증식 방지: 5000건 초과 시 오래된 것부터 정리
    if len(state["seen"]) > 5000:
        keys = list(state["seen"].keys())
        for k in keys[: len(keys) - 4000]:
            del state["seen"][k]

    state["initialized"] = True
    save_state(cfg["state_file"], state)
    print(f"=== 완료: 신규 {new_count}건, 전송 {sent_count}건 ===")


if __name__ == "__main__":
    main()
