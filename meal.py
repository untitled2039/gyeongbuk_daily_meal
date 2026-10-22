"""경북학숙 주간메뉴(kydel.or.kr) 크롤러.

사이트 특징
- 하루에 게시글 1개, 제목은 "10월 22일" 형식 (연도 없음)
- 본문은 빈 줄로 나뉜 3블록: 조식 / 중식 / 석식
- wr_id 가 날짜순으로 1씩 증가 (주말 포함) → 기준 글에서 날짜 차이만큼 이동해서 찾음
- 목록 페이지와 RSS 는 막혀 있어서 사용하지 않음
"""
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

KST = timezone(timedelta(hours=9))
BASE_URL = "https://kydel.or.kr/pg/bbs/board.php?bo_table=life2&wr_id={}"
STATE_FILE = os.environ.get("MEAL_STATE_FILE", "meal_state.json")

# 확인된 기준점: wr_id 4616 = 2026-10-22
DEFAULT_ANCHOR = {"wr_id": 4616, "date": "2026-10-22"}

MEAL_NAMES = ["조식", "중식", "석식"]
HEADERS = {"User-Agent": "Mozilla/5.0 (meal-bot)"}

_lock = threading.Lock()
_cache: dict[str, dict] = {}  # "YYYY-MM-DD" -> {"meals": {...}, "url": ..., "fetched": ts}


def today_kst() -> date:
    return datetime.now(KST).date()


# ---------------- 상태(기준점) 저장 ----------------
def _load_anchor() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return dict(DEFAULT_ANCHOR)


def _save_anchor(wr_id: int, d: date) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"wr_id": wr_id, "date": d.isoformat()}, f)
    except OSError:
        pass


# ---------------- 파싱 ----------------
def _parse_title_date(soup: BeautifulSoup, near: date) -> date | None:
    """제목에서 'M월 D일' 을 찾아 near 에 가장 가까운 연도로 date 를 만든다."""
    candidates = []
    if soup.title and soup.title.string:
        candidates.append(soup.title.string)
    for sel in ["#bo_v_title", ".bo_v_tit", "h2", "h1"]:
        for el in soup.select(sel):
            candidates.append(el.get_text(" ", strip=True))
    for text in candidates:
        m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
        if not m:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        best = None
        for y in (near.year - 1, near.year, near.year + 1):
            try:
                d = date(y, month, day)
            except ValueError:
                continue
            if best is None or abs((d - near).days) < abs((best - near).days):
                best = d
        if best:
            return best
    return None


def _content_lines(soup: BeautifulSoup) -> list[str]:
    """본문을 줄 단위 텍스트로. 빈 줄은 '' 로 유지(식사 구분자)."""
    el = None
    for sel in ["#bo_v_con", ".bo_v_con", "#bo_v_atc", ".view_content", ".board_view .cont"]:
        el = soup.select_one(sel)
        if el:
            break

    if el is None:
        # 스킨이 달라 선택자가 안 맞을 때: '본문' 제목 ~ '이전글' 사이 텍스트 사용
        text = soup.get_text("\n")
        m = re.search(r"본문\s*\n(.*?)(?:이전글|다음글|목록)", text, re.S)
        raw = m.group(1) if m else ""
    else:
        for br in el.find_all("br"):
            br.replace_with("\n")
        for blk in el.find_all(["p", "div", "li", "tr"]):
            blk.insert_after("\n")
        raw = el.get_text()

    lines = [ln.replace("\xa0", " ").strip() for ln in raw.split("\n")]
    return lines


def _split_meals(lines: list[str]) -> dict[str, list[str]]:
    groups, cur = [], []
    for ln in lines:
        if ln:
            cur.append(ln)
        elif cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)

    if len(groups) == 3:
        return dict(zip(MEAL_NAMES, groups))

    # 빈 줄 구분이 없을 때: 줄 수가 3의 배수면 균등 분할
    flat = [x for g in groups for x in g]
    if flat and len(groups) == 1 and len(flat) % 3 == 0:
        n = len(flat) // 3
        return dict(zip(MEAL_NAMES, [flat[:n], flat[n:2 * n], flat[2 * n:]]))

    if len(groups) > 3:  # 앞 3개만 조/중/석, 나머지는 기타
        out = dict(zip(MEAL_NAMES, groups[:3]))
        out["기타"] = [x for g in groups[3:] for x in g]
        return out

    return {"식단": flat} if flat else {}


def fetch_post(wr_id: int, near: date):
    """(날짜, 식단dict, url) 반환. 글이 없으면 (None, None, url)."""
    url = BASE_URL.format(wr_id)
    r = requests.get(url, headers=HEADERS, timeout=4)
    r.encoding = "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    d = _parse_title_date(soup, near)
    if d is None:
        return None, None, url
    return d, _split_meals(_content_lines(soup)), url


# ---------------- 날짜 → 글 찾기 ----------------
def find_meal(target: date, max_steps: int = 8) -> dict | None:
    key = target.isoformat()
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit["fetched"] < 3600:
            return hit

    anchor = _load_anchor()
    a_id, a_date = anchor["wr_id"], date.fromisoformat(anchor["date"])
    guess = a_id + (target - a_date).days
    tried = set()
    deadline = time.time() + 3.5  # 카카오 스킬 응답 제한(5초) 안에 끝내기
    misses = 0

    for _ in range(max_steps):
        if guess in tried or guess <= 0 or time.time() > deadline:
            break
        tried.add(guess)
        try:
            d, meals, url = fetch_post(guess, target)
        except requests.RequestException:
            return None
        if d is None:  # 아직 안 올라온 글 / 삭제된 글 → 한 칸 앞으로
            misses += 1
            if misses >= 3:  # 연속으로 없으면 아직 등록 전으로 판단
                break
            guess -= 1
            continue
        misses = 0
        _save_anchor(guess, d)  # 날짜 확인된 글은 새 기준점으로
        with _lock:
            _cache[d.isoformat()] = {"meals": meals, "url": url, "fetched": time.time(), "date": d}
        if d == target:
            return _cache[key]
        diff = (target - d).days
        guess = guess + diff if diff else guess + 1
    return None


def format_meal(target: date, info: dict | None, only=None) -> str:
    """only: None(전체) / "조식" / ["조식", "중식"]"""
    if isinstance(only, str):
        only = [only]
    wd = "월화수목금토일"[target.weekday()]
    label = "·".join(only) if only else "식단"
    head = f"🍚 {target.month}월 {target.day}일({wd}) {label}"
    if not info or not info["meals"]:
        return f"{head}\n\n등록된 식단이 아직 없어요."
    meals = info["meals"]
    if only and not any(n in meals for n in only):
        # 빈 줄 구분이 깨져 조/중/석을 못 나눈 날은 전체를 보여줌
        only = None
    parts = [head]
    emoji = {"조식": "🌅", "중식": "☀️", "석식": "🌙"}
    for name, items in meals.items():
        if only and name not in only:
            continue
        parts.append(f"{emoji.get(name, '🍽')} {name}\n" + "\n".join(f"· {x}" for x in items))
    return "\n\n".join(parts)


# ---------------- 사용자 문장 해석 ----------------
MEAL_WORDS = {
    "조식": ["아침", "조식", "모닝"],
    "중식": ["점심", "중식", "낮"],
    "석식": ["저녁", "석식", "밤"],
}
DAY_WORDS = {"그제": -2, "어제": -1, "오늘": 0, "내일": 1, "모레": 2}


def parse_utterance(text: str):
    """'내일 아침', '10월 25일 저녁', '아침 점심' → (날짜, ['조식', ...] 또는 None)"""
    text = text or ""
    target = today_kst()
    for word, offset in DAY_WORDS.items():
        if word in text:
            target = today_kst() + timedelta(days=offset)
    m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    if m:
        try:
            target = date(today_kst().year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    only = [name for name, words in MEAL_WORDS.items() if any(w in text for w in words)]
    return target, only or None
