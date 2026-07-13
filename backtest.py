#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backtest.py — 과거 레이드(세르카) 공략글로 AI 채점기의 정확도를 검증한다.

핵심 질문: "우리 AI가 진짜 좋은 공략글에 높은 점수를 주는가?"
  → 지금까지는 '뻘글에 낮은 점수를 준다'만 확인했고,
    '좋은 글에 높은 점수를 준다'는 한 번도 검증하지 못했다.
    이게 틀리면 8/5에 최고의 파훼법 글이 버려진다.

동작:
  1. backtest_urls.txt 에서 인벤 글 URL과 사람이 붙인 정답 라벨을 읽는다
  2. 각 글의 제목·본문을 가져온다
  3. 운영 코드와 **완전히 동일한** claude_evaluate() 로 채점한다
     (같은 모델 claude-haiku-4-5, 같은 프롬프트 구조, 주제만 세르카로 교체)
  4. 사람 라벨 vs AI 점수를 비교해 리포트를 출력한다

디스코드로 아무것도 보내지 않는다. state.json도 건드리지 않는다. 읽기 전용.
"""

import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

# 운영 코드를 그대로 재사용 — 프롬프트가 갈리면 실험이 무의미해진다
from inven_watcher import BOARD, CONFIG, TOPICS, claude_evaluate, fetch_post_body, http_get

URLS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_urls.txt")
BACKTEST_TOPIC = "세르카"   # 또는 "배마낙원"

# 운영 컷 (inven_watcher.py 와 동일하게 유지할 것)
CUTS = {"6271": 6, "4821": 4, "5342": 5}
BOARD_NAMES = BOARD
POST_URL_RE = re.compile(r"/board/lostark/(\d+)/(\d+)")


def load_urls(path):
    """
    backtest_urls.txt 형식 (한 줄에 하나):
        https://www.inven.co.kr/board/lostark/4821/109163 | 좋음
        https://www.inven.co.kr/board/lostark/6271/3137889 | 뻘글
    '|' 뒤 라벨은 생략 가능. '#' 로 시작하는 줄과 빈 줄은 무시.
    """
    if not os.path.exists(path):
        print(f"[오류] {path} 가 없습니다.", file=sys.stderr)
        sys.exit(1)
    items = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "|" in line:
                url, label = [x.strip() for x in line.split("|", 1)]
            else:
                url, label = line, ""
            if not url.startswith("http"):
                print(f"[경고] URL 형식이 아님, 건너뜀: {line[:60]}", file=sys.stderr)
                continue
            items.append({"url": url, "label": label})
    return items


def fetch_title(html):
    """글 제목 추출. <title> 태그에서 인벤 접두/접미사를 벗겨낸다. (이미 받은 HTML 재사용)"""
    soup = BeautifulSoup(html, "html.parser")

    # og:title 우선
    og = soup.find("meta", attrs={"property": "og:title"})
    raw = og["content"] if og and og.get("content") else (soup.title.string if soup.title else "")
    raw = (raw or "").strip()

    # "로스트아크 인벤 : 제목 - 로스트아크 인벤 팁과 노하우 게시판 - 로스트아크 인벤"
    raw = re.sub(r"^로스트아크 인벤\s*:\s*", "", raw)
    raw = re.sub(r"\s*-\s*로스트아크 인벤.*$", "", raw)
    return raw.strip() or "(제목 추출 실패)"


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[오류] ANTHROPIC_API_KEY 가 없습니다. 백테스트는 API가 필수입니다.", file=sys.stderr)
        sys.exit(1)

    # 채점 주제를 세르카로 교체 (구조는 벨가르딘 프리셋과 동일)
    cfg = dict(CONFIG)
    cfg["topic"] = TOPICS[BACKTEST_TOPIC]

    items = load_urls(URLS_FILE)
    session = requests.Session()
    session.headers.update({"User-Agent": cfg["user_agent"]})

    print("=" * 78)
    print(f"백테스트 / 스윕 — 운영 코드와 동일한 프롬프트·모델로 채점")
    print(f"주제: {BACKTEST_TOPIC}  |  모델: {cfg['claude_model']}  |  대상 {len(items)}건")
    print("=" * 78)

    rows = []
    for i, item in enumerate(items, 1):
        m = POST_URL_RE.search(item["url"])
        board = m.group(1) if m else "?"
        board_name = BOARD_NAMES.get(board, "기타")

        try:
            html = http_get(item["url"], session)   # 글 1개당 요청 1번만
            title = fetch_title(html)
            content = fetch_post_body(item["url"], session, html=html)
        except Exception as e:  # noqa: BLE001
            print(f"[{i:2}] 요청 실패: {e}", file=sys.stderr)
            rows.append({**item, "board": board, "board_name": board_name,
                         "title": "(요청 실패)", "score": None, "category": "-"})
            continue

        body = content["text"]
        media = {"images": content["images"], "videos": content["videos"]}
        ev = claude_evaluate(title, body, api_key, cfg, media)
        score = ev["score"] if ev else None
        cat = ev["category"] if ev else "-"
        summary = ev["summary"] if ev else []

        cut = CUTS.get(board, 6)
        media_guide = (media["images"] >= 5 or media["videos"] >= 1) and (score is None or score >= 2)
        ok = (score is not None and score >= cut) or media_guide
        passed = "통과(📷미디어)" if (ok and not (score is not None and score >= cut)) else ("통과" if ok else "차단")
        m_txt = f" img{media['images']}/vid{media['videos']}" if (media['images'] or media['videos']) else ""
        print(f"[{i:2}] {score if score is not None else '??':>2}/10  {cat:<4}  "
              f"{board_name}(컷{cut})  {passed}{m_txt}  | {title[:40]}")
        for s in summary:
            print(f"       · {s}")

        rows.append({**item, "board": board, "board_name": board_name, "title": title,
                     "score": score, "category": cat, "cut": cut,
                     "passed": ok, "media": media,
                     "body_len": len(body)})
        time.sleep(3)

    # ── 리포트 ──────────────────────────────────────────
    scored = [r for r in rows if r.get("score") is not None]
    failed = [r for r in rows if r.get("score") is None]

    print("\n" + "=" * 78)
    print("결과 요약")
    print("=" * 78)
    if not scored:
        print("채점된 글이 없습니다. 요청 실패 또는 API 오류를 확인하세요.")
        sys.exit(1)

    avg = sum(r["score"] for r in scored) / len(scored)
    print(f"채점 성공 {len(scored)}건 / 실패 {len(failed)}건 | 평균 점수 {avg:.1f}")

    good = [r for r in scored if r["label"] in ("좋음", "good", "")]
    junk = [r for r in scored if r["label"] in ("뻘글", "junk")]

    if good:
        ga = sum(r["score"] for r in good) / len(good)
        gp = sum(1 for r in good if r["passed"])
        print(f"\n[좋은 글로 라벨링한 {len(good)}건]")
        print(f"  평균 {ga:.1f}점 · 운영 컷 통과 {gp}/{len(good)} ({gp/len(good)*100:.0f}%)  ← 재현율")
    if junk:
        ja = sum(r["score"] for r in junk) / len(junk)
        jb = sum(1 for r in junk if not r["passed"])
        print(f"\n[뻘글로 라벨링한 {len(junk)}건]")
        print(f"  평균 {ja:.1f}점 · 운영 컷 차단 {jb}/{len(junk)} ({jb/len(junk)*100:.0f}%)  ← 정확도")

    missed = [r for r in good if not r["passed"]]
    print("\n" + "-" * 78)
    if missed:
        print(f"⚠️  놓쳤을 좋은 글 {len(missed)}건 — 8/5에 이런 글이 버려집니다:")
        for r in missed:
            m = r.get("media", {})
            print(f"   {r['score']}/10 (컷 {r['cut']}) · 본문 {r['body_len']}자 · img{m.get('images',0)}/vid{m.get('videos',0)} · {r['title'][:46]}")
            print(f"        {r['url']}")
        print("\n→ 조치: 컷을 낮추거나, 프롬프트의 점수 기준을 손봐야 합니다.")
    else:
        print("✅ 놓친 좋은 글 없음. 현재 컷으로 좋은 공략글을 전부 잡아냅니다.")

    leaked = [r for r in junk if r["passed"]]
    if leaked:
        print(f"\n⚠️  통과된 뻘글 {len(leaked)}건 (알림 노이즈):")
        for r in leaked:
            print(f"   {r['score']}/10 · {r['title'][:50]}")

    # 컷별 시뮬레이션 — 최적 컷 탐색
    if good:
        print("\n" + "-" * 78)
        print("컷 조정 시뮬레이션 (좋은 글 기준 통과율)")
        for c in range(2, 10):
            p = sum(1 for r in good if r["score"] >= c)
            bar = "█" * p + "·" * (len(good) - p)
            print(f"  컷 {c}점: {p}/{len(good)} 통과  {bar}")

    print("=" * 78)


if __name__ == "__main__":
    main()
