#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
inven_watcher.py — 로스트아크 인벤 다중 주제 감시기 (v2)

v1과의 차이: 하나의 주제만 보던 구조 → 여러 주제를 동시에 감시하는 구조.
  · 주제(WATCH)마다 게시판 / 키워드 / AI 평가 기준 / 점수 컷 / 디스코드 채널을 따로 갖는다.
  · 주제를 켜고 끄는 건 enabled 플래그 한 줄.

환경변수:
  ANTHROPIC_API_KEY    : Claude API 키 (필수)
  DISCORD_WEBHOOK_URL  : 기본 디스코드 웹훅 (필수)
  DISCORD_WEBHOOK_BM   : 배마 낙원 전용 채널 (선택. 없으면 기본 웹훅으로 감)

주의: 인벤 콘텐츠는 저작권 보호 대상.
      본문을 퍼가지 말고 "제목 + 짧은 AI 요약 + 원문 링크"만 전송한다.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ══════════════════════════════════════════════════════════════
#  게시판 ID (웹 검색으로 실제 확인함)
# ══════════════════════════════════════════════════════════════
BOARD = {
    "6271": "자유게시판",
    "4821": "팁과 노하우",
    "5342": "배틀마스터",   # 클래스 게시판. 배마 낙원 공략의 본진.
}

# ══════════════════════════════════════════════════════════════
#  테스트 모드
#    True  = 점수 컷 무시, 키워드만 맞으면 전송 (주제당 최대 3건)
#    False = 운영 모드
# ══════════════════════════════════════════════════════════════
TEST_MODE = False

# ══════════════════════════════════════════════════════════════
#  감시 주제 목록 — enabled 를 켜고 끄면 됩니다
# ══════════════════════════════════════════════════════════════
WATCHES = [
    # ── 리허설 (~8/4). 벨가르딘 켜면 꺼도 됩니다 ──────────────
    {
        "name": "차원술사",
        "enabled": True,
        "boards": ["6271", "4821"],
        "keywords": ["차원술사", "시간 관리자", "공간 검사"],
        "min_score": {"6271": 6, "4821": 4},
        "webhook_env": "DISCORD_WEBHOOK_URL",
        "topic": (
            "신규 클래스 '차원술사' 관련 유용 정보 "
            "(스킬·트라이포드, 각인·세팅, 아크그리드, 딜사이클, 육성 팁, 성능 분석. "
            "각성은 '시간 관리자'와 '공간 검사' 두 가지)"
        ),
    },

    # ── 본작전 (8/5 출시). 그날 enabled 를 True 로 ────────────
    #   약칭 대응: 직전 레이드 세르카는 노르카/하르카/나르카로 불렸다.
    #   '가르딘'을 넣으면 벨가르딘/노가르딘/하가르딘/나가르딘을 한 번에 잡는다.
    {
        "name": "벨가르딘",
        "enabled": False,
        "boards": ["6271", "4821", "5342"],
        "keywords": ["가르딘", "벨딘", "페투스", "크라그마"],
        "min_score": {"6271": 6, "4821": 4, "5342": 5},
        "webhook_env": "DISCORD_WEBHOOK_URL",
        "topic": (
            "'죽음의 계율자 벨가르딘' 레이드 공략 정보 "
            "(기믹 파훼법, 1·2관문별 팁, 짤패턴, 대난투, 추천 세팅, 트라이 중 검증된 공략. "
            "2관문 보스는 '페투스 안 크라그마')"
        ),
    },

    # ── 배마 낙원 증명 랭킹 ───────────────────────────────────
    #   ★ 세르카 백테스트 결과 반영 후 켤 것 (enabled: True 로)
    #   ★ 낙원은 '풀 보정' 콘텐츠다. 각인·아크그리드·보석·엘릭서가 전부 미적용이라
    #     낙원 전용 스킬트리와 유산 세팅만이 유효하다.
    #     따라서 일반 배마 빌드 공략(초심배마 가이드 등)은 여기서 '무관'으로 처리해야 한다.
    #     이 구분을 프롬프트에 명시하지 않으면 AI가 엉뚱한 글을 높게 평가한다.
    {
        "name": "배마낙원",
        "enabled": False,
        "boards": ["5342", "4821"],
        "keywords": ["낙원", "증명", "보주", "낙원력"],
        "min_score": {"5342": 5, "4821": 5},
        "webhook_env": "DISCORD_WEBHOOK_BM",   # 없으면 기본 웹훅으로 자동 폴백
        "topic": (
            "'낙원 - 증명' 콘텐츠에서 배틀마스터로 주간 랭킹 상위권에 들기 위한 정보. "
            "중요: 낙원은 각인·아크 그리드·보석·엘릭서·초월이 전혀 적용되지 않는 '풀 보정' 콘텐츠다. "
            "따라서 유효한 정보는 [낙원 전용 스킬트리/스킬코드, 유산 세팅과 강화 우선순위, "
            "보주 선택, 증명 단계별 보스 패턴과 클리어 타임 단축법, 낙원력 올리는 법, 랭킹작 전략]이다. "
            "반대로 일반 레이드용 각인·아크그리드·보석·초월 세팅 글은 낙원과 무관하므로 낮게 평가하라. "
            "타 직업 글이라도 전직업 정리본이나 낙원 시스템 자체의 공략이면 유용하다."
        ),
    },
]


# ══════════════════════════════════════════════════════════════
#  백테스트/스윕 전용 주제 사전 (backtest.py 가 가져다 씀)
#  운영 WATCHES 의 topic 을 그대로 재사용해야 실험이 유효하다.
# ══════════════════════════════════════════════════════════════
TOPICS = {w["name"]: w["topic"] for w in WATCHES}
# 직전 그림자 레이드(2026-01-07 출시). 벨가르딘 프롬프트와 구조를 똑같이 맞춘 대조군.
TOPICS["세르카"] = (
    "'고통의 마녀, 세르카' 레이드 공략 정보 "
    "(기믹 파훼법, 1·2관문별 팁, 짤패턴, 대난투, 추천 세팅, 트라이 중 검증된 공략. "
    "2관문 보스는 '코르부스 툴 라크')"
)

# ══════════════════════════════════════════════════════════════
#  공통 설정
# ══════════════════════════════════════════════════════════════
CONFIG = {
    "list_url": "https://www.inven.co.kr/board/lostark/{board_id}",
    "recommend_override": 10,      # 추천 10 이상이면 점수 미달이어도 전송
    "body_max_chars": 4000,
    "request_delay": 1.5,
    "max_sends_per_run": 15,       # 주제별. 초과분은 다음 실행으로 이월(유실 없음)
    "claude_model": "claude-haiku-4-5-20251001",
    "claude_max_tokens": 600,
    "user_agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "state_file": os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"),
}
if TEST_MODE:
    CONFIG["max_sends_per_run"] = 3

KST = timezone(timedelta(hours=9))
POST_URL_RE = re.compile(r"/board/lostark/(\d+)/(\d+)")


# ══════════════════════════════════════════════════════════════
#  상태 관리 (주제별 seen / 주제별 첫 실행 플래그)
# ══════════════════════════════════════════════════════════════
def load_state(path):
    if not os.path.exists(path):
        return {"seen": {}, "initialized": {}}
    with open(path, encoding="utf-8") as f:
        state = json.load(f)

    # v1 → v2 마이그레이션.
    # v1 형식: seen 키가 "6271:123", initialized 가 bool.
    # 그대로 두면 v2가 전부 '신규 글'로 보고 알림을 쏟아낸다.
    if isinstance(state.get("initialized"), bool):
        old_init = state["initialized"]
        migrated = {}
        for k, v in state.get("seen", {}).items():
            # v1 시절 돌던 주제는 '차원술사' 하나뿐이었다
            migrated[f"차원술사|{k}"] = v
        state["seen"] = migrated
        state["initialized"] = {"차원술사": old_init}
        print("[안내] 상태 파일을 v1 → v2 형식으로 변환했습니다.")

    state.setdefault("seen", {})
    state.setdefault("initialized", {})
    return state


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ══════════════════════════════════════════════════════════════
#  인벤 파싱
# ══════════════════════════════════════════════════════════════
def http_get(url, session):
    r = session.get(url, timeout=15)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() in ("iso-8859-1",):
        r.encoding = r.apparent_encoding
    return r.text


def parse_board_list(html, board_id):
    """
    CSS 클래스에 기대지 않고 글 URL 패턴으로 앵커를 수집하는 방어적 파서.
    인벤 마크업이 바뀌어도 잘 버틴다.
    """
    soup = BeautifulSoup(html, "html.parser")
    posts = {}
    for a in soup.find_all("a", href=True):
        m = POST_URL_RE.search(a["href"])
        if not m or m.group(1) != board_id:
            continue
        post_id = m.group(2)
        title = a.get_text(" ", strip=True)
        title = re.sub(r"\s*\[(?:댓글\s*)?\d+\]\s*$", "", title)  # 댓글수 노이즈 제거
        if not title or len(title) < 2:
            continue

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

        if post_id in posts:  # 같은 글에 앵커가 여러 개 → 필드별 병합
            ex = posts[post_id]
            if len(title) > len(ex["title"]):
                ex["title"] = title
            if ex["recommend"] is None and recommend is not None:
                ex["recommend"] = recommend
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
    html = http_get(url, session)
    soup = BeautifulSoup(html, "html.parser")
    for finder in (
        lambda s: s.find(id="powerbbsContent"),
        lambda s: s.find(class_="articleContent"),
        lambda s: s.find(id="BoardContent"),
        lambda s: s.find(class_="contentBody"),
    ):
        node = finder(soup)
        if node:
            text = node.get_text("\n", strip=True)
            if len(text) > 30:
                return text
    return soup.get_text("\n", strip=True)


# ══════════════════════════════════════════════════════════════
#  Claude 채점
# ══════════════════════════════════════════════════════════════
def claude_evaluate(title, body, api_key, cfg):
    """반환: {"score": 0~10, "category": str, "summary": [3줄]} / 실패 시 None"""
    body = body[: cfg["body_max_chars"]]
    system = (
        "당신은 로스트아크 유저의 정보 큐레이터입니다. "
        "인벤 게시글이 다음 주제에 실질적으로 도움이 되는 정보인지 평가하세요.\n"
        f"주제: {cfg['topic']}\n"
        "점수 기준: 9~10 구체적이고 검증된 핵심 정보, 7~8 유용한 팁·분석, "
        "5~6 부분적으로 유용, 3~4 후기·감상 위주, 0~2 뻘글·어그로·잡담·주제 무관.\n"
        "본문이 짧아도 핵심 수치나 파훼법이 담겨 있으면 높게 평가하세요. "
        "길이가 아니라 정보 밀도로 판단합니다.\n"
        "반드시 아래 JSON만 출력하세요. 다른 텍스트, 마크다운 펜스 금지.\n"
        '{"score": <0-10 정수>, "category": "<기믹|공략|세팅|정보|후기|뻘글|기타>", '
        '"summary": ["<핵심1>", "<핵심2>", "<핵심3>"]}'
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": cfg["claude_model"],
                "max_tokens": cfg["claude_max_tokens"],
                "system": system,
                "messages": [{"role": "user", "content": f"제목: {title}\n\n본문:\n{body}"}],
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"```(?:json)?|```", "", text).strip()
        res = json.loads(text)
        return {
            "score": int(res.get("score", 0)),
            "category": str(res.get("category", "기타")),
            "summary": [str(s) for s in res.get("summary", [])][:3],
        }
    except Exception as e:  # noqa: BLE001
        print(f"    [경고] Claude 평가 실패: {e}", file=sys.stderr)
        return None


# ══════════════════════════════════════════════════════════════
#  디스코드 전송
# ══════════════════════════════════════════════════════════════
def send_discord(webhook_url, post, watch_name, evaluation):
    reco = post.get("recommend")
    tag = ""
    if isinstance(reco, int):
        if reco >= 30:
            tag = " 🔥30추+"
        elif reco >= 10:
            tag = " 🔥10추+"

    board_name = BOARD.get(post["board"], "기타")
    if evaluation:
        score = evaluation["score"]
        desc = "\n".join(f"• {s}" for s in evaluation["summary"])
        footer = f"[{watch_name}] {board_name} · {evaluation['category']} · 정보성 {score}/10"
        color = 0x2ECC71 if score >= 8 else 0xF1C40F if score >= 6 else 0x95A5A6
    else:
        desc = "(AI 요약 실패 — 원문 링크로 직접 확인)"
        footer = f"[{watch_name}] {board_name} · 평가 불가"
        color = 0x95A5A6

    if TEST_MODE:
        footer = "🧪 테스트 · " + footer

    embed = {
        "title": (post["title"] + tag)[:250],
        "url": post["url"],
        "description": desc[:2000],
        "color": color,
        "footer": {"text": footer},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    r = requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    if r.status_code == 429:
        time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=15)
    elif r.status_code >= 400:
        print(f"    [경고] 디스코드 전송 실패 {r.status_code}: {r.text[:150]}", file=sys.stderr)


# ══════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════
def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    default_hook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not default_hook:
        print("[오류] DISCORD_WEBHOOK_URL 환경변수가 없습니다.", file=sys.stderr)
        sys.exit(1)
    if not api_key:
        print("[경고] ANTHROPIC_API_KEY 없음 → 제목+링크만 전송합니다.", file=sys.stderr)

    cfg_base = CONFIG
    state = load_state(cfg_base["state_file"])
    session = requests.Session()
    session.headers.update({"User-Agent": cfg_base["user_agent"]})

    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M")
    active = [w for w in WATCHES if w["enabled"]]
    mode = " [테스트 모드]" if TEST_MODE else ""
    print(f"═══ 인벤 감시 {now} KST{mode} ═══")
    print(f"활성 주제: {', '.join(w['name'] for w in active) or '(없음)'}")

    list_cache = {}   # 같은 게시판을 여러 주제가 보면 목록은 한 번만 가져온다
    totals = {"new": 0, "sent": 0, "deferred": 0}

    for watch in active:
        wname = watch["name"]
        first_run = not state["initialized"].get(wname, False)
        hook = os.environ.get(watch["webhook_env"], "") or default_hook
        if watch["webhook_env"] != "DISCORD_WEBHOOK_URL" and not os.environ.get(watch["webhook_env"]):
            print(f"\n[{wname}] {watch['webhook_env']} 미설정 → 기본 채널로 전송합니다.")

        cfg = dict(cfg_base)
        cfg["topic"] = watch["topic"]

        print(f"\n── [{wname}] {'첫 실행(기록만, 알림 없음)' if first_run else '운영'} ──")
        sent = 0

        for board_id in watch["boards"]:
            if board_id not in list_cache:
                try:
                    html = http_get(cfg["list_url"].format(board_id=board_id), session)
                    list_cache[board_id] = parse_board_list(html, board_id)
                    time.sleep(cfg["request_delay"])
                except Exception as e:  # noqa: BLE001
                    print(f"  [오류] {BOARD.get(board_id)} 목록 실패: {e}", file=sys.stderr)
                    list_cache[board_id] = []
            posts = list_cache[board_id]

            if not posts:
                print(f"  [경고] {BOARD.get(board_id)}: 파싱 0건 — 차단/구조변경 확인 필요", file=sys.stderr)
                continue
            print(f"  {BOARD.get(board_id)}: 목록 {len(posts)}건")

            for post in posts:
                key = f"{wname}|{board_id}:{post['post_id']}"
                if key in state["seen"]:
                    continue
                totals["new"] += 1

                matched = any(k in post["title"] for k in watch["keywords"])

                # 첫 실행이거나 키워드 미매치 → 기록만
                if first_run or not matched:
                    state["seen"][key] = {"t": now, "title": post["title"][:80]}
                    continue

                # 전송 상한 → seen에 넣지 않고 다음 실행으로 이월 (유실 방지)
                if sent >= cfg["max_sends_per_run"]:
                    totals["deferred"] += 1
                    continue

                print(f"    → 매치 (추천 {post.get('recommend')}): {post['title'][:50]}")
                time.sleep(cfg["request_delay"])

                ev = None
                if api_key:
                    try:
                        body = fetch_post_body(post["url"], session)
                    except Exception as e:  # noqa: BLE001
                        print(f"    [경고] 본문 실패: {e}", file=sys.stderr)
                        body = ""
                    if body:
                        ev = claude_evaluate(post["title"], body, api_key, cfg)

                cut = 0 if TEST_MODE else watch["min_score"].get(board_id, 6)
                reco = post.get("recommend") or 0
                should = (ev is None) or ev["score"] >= cut or reco >= cfg["recommend_override"]

                if should:
                    send_discord(hook, post, wname, ev)
                    sent += 1
                    totals["sent"] += 1
                    time.sleep(1)
                else:
                    print(f"    → 스킵 (점수 {ev['score']} < {cut}, 추천 {reco})")

                state["seen"][key] = {"t": now, "title": post["title"][:80]}

        state["initialized"][wname] = True
        print(f"  [{wname}] 전송 {sent}건")

    # seen 무한 증식 방지
    if len(state["seen"]) > 8000:
        keys = list(state["seen"].keys())
        for k in keys[: len(keys) - 6000]:
            del state["seen"][k]

    save_state(cfg_base["state_file"], state)
    tail = f", 이월 {totals['deferred']}건" if totals["deferred"] else ""
    print(f"\n═══ 완료: 신규 {totals['new']}건, 전송 {totals['sent']}건{tail} ═══")


if __name__ == "__main__":
    main()
