"""「その他」で入力された自由文から (日付, 食事区分) を推定する（ゆるい日本語対応）.

日付: 今日 / きょう / 昨日 / きのう / おととい / 一昨日 / N日前 / 二日前 / 10/2 / 2026-10-02
区分: 朝 / 朝食 / あさ / ひる / 昼 / 昼食 / ランチ / 夕 / 夕方 / 夕食 / 夜 / 夜食 / 間食 / おやつ（どちらも snack）

判定できない項目は None を返す（勝手に今日・間食へ丸めない）.
"""
import re
from datetime import date, timedelta

from .dates import today_jst_date

_KANJI = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_SLOT_PATTERNS = (
    (r"朝食|朝|あさ|モーニング", "breakfast"),
    (r"昼食|昼|ひる|ランチ|昼ごはん|昼ご飯", "lunch"),
    (r"夕食|夕方|夕|ばん|晩|ディナー|夜ごはん|夜ご飯", "dinner"),
    (r"夜食|夜|よる|深夜", "snack"),
    (r"間食|おやつ|オヤツ|スナック|間", "snack"),
)


def _kanji_num(t: str):
    """漢数字（二 / 十 / 二十 など）を int にする。不能なら None."""
    if not t:
        return None
    if t.isdigit():
        return int(t)
    if "十" in t:
        a, _, b = t.partition("十")
        a = _KANJI.get(a, 1) if a else 1
        b = _KANJI.get(b, 0) if b else 0
        return a * 10 + b
    v = 0
    for ch in t:
        if ch not in _KANJI:
            return None
        v = v * 10 + _KANJI[ch]
    return v


def parse_meal_when(text: str):
    """(date_iso, slot) を返す。判定不能な項目は None."""
    t = (text or "").strip()
    if not t:
        return None, None
    today = today_jst_date()
    d = None

    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            d = None

    if d is None:
        m = re.search(r"(\d{1,2})\s*[/月]\s*(\d{1,2})", t)
        if m:
            try:
                cand = date(today.year, int(m.group(1)), int(m.group(2)))
                if cand > today:
                    cand = date(today.year - 1, int(m.group(1)), int(m.group(2)))
                d = cand
            except ValueError:
                d = None

    if d is None:
        if re.search(r"おととい|一昨日|おとつい", t):
            d = today - timedelta(days=2)
        elif re.search(r"昨日|きのう|前日", t):
            d = today - timedelta(days=1)
        elif re.search(r"今日|きょう|本日", t):
            d = today

    if d is None:
        m = re.search(r"([0-9一二三四五六七八九十]+)\s*日前", t)
        if m:
            n = _kanji_num(m.group(1))
            if n:
                d = today - timedelta(days=n)

    slot = None
    for pat, val in _SLOT_PATTERNS:
        if re.search(pat, t):
            slot = val
            break

    return (d.isoformat() if d else None), slot
