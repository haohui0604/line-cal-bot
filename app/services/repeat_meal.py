"""「いついつの◯◯と同じ」で過去の食事をそのまま複製登録する.

方針:
- 日付の解決はコード（JST = Asia/Tokyo）で行う。AIには「解釈」だけを任せる。
- DBから該当する entries を取得し、品目・kcal・PFC・食塩を「そのまま」コピーする。
- 該当なし／日付や区分が特定できないときは推測せず、ユーザーに確認する。
"""
import logging
import re
import time
from datetime import date, timedelta

from linebot.models import TextSendMessage

from app.services.dates import today_jst, today_jst_date

logger = logging.getLogger(__name__)

# 「同じ」系の言い回し（これが無ければ複製機能として扱わない）
_REPEAT_PAT = re.compile(r"同じ|おなじ|一緒|いっしょ|同様|そのまま|コピー|と同")
# 「同じくらい食べた」等は食事の報告なので複製扱いしない
_EXCLUDE_PAT = re.compile(r"同じくらい|同じ位|同じぐらい")

_ISO_PAT = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_SLASH_PAT = re.compile(r"(\d{1,2})\s*[/月]\s*(\d{1,2})\s*日?")
_DAYS_AGO_PAT = re.compile(r"([0-9０-９一二三四五六七八九十]+)\s*日\s*前")
_WD_PAT = re.compile(r"(先々週|先週|今週|来週|再来週)?\s*の?\s*([月火水木金土日])曜")

_WD_JP = "月火水木金土日"

_SLOT_PATTERNS = (
    (r"朝食|朝|あさ|モーニング", "breakfast"),
    (r"昼食|昼|ひる|ランチ|昼ごはん|昼ご飯|昼飯", "lunch"),
    (r"夕食|夕方|夕|ばん|晩|ディナー|夜ごはん|夜ご飯|晩ごはん|晩ご飯", "dinner"),
    (r"夜食|深夜|よる|夜", "snack"),
    (r"間食|おやつ|オヤツ|スナック", "snack"),
)

_SLOT_JP = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食", "snack": "間食"}

_MEAL_MAP = {
    "breakfast": "breakfast", "lunch": "lunch", "dinner": "dinner",
    "snack": "snack", "朝食": "breakfast", "朝": "breakfast",
    "昼食": "lunch", "昼": "lunch", "夕食": "dinner", "夕": "dinner",
    "夜食": "snack", "間食": "snack", "おやつ": "snack",
}


def looks_like_repeat(text: str) -> bool:
    """複製登録を意図していそうな発言か（キーワード判定）."""
    t = text or ""
    return bool(_REPEAT_PAT.search(t)) and not _EXCLUDE_PAT.search(t)


def _kanji_num(t: str):
    from app.services.meal_when import _kanji_num as _k
    return _k(t)


def _label(d: date, today: date) -> str:
    if d == today:
        return f"今日（{d.isoformat()}）"
    if d == today - timedelta(days=1):
        return f"昨日（{d.isoformat()}）"
    if d == today - timedelta(days=2):
        return f"おととい（{d.isoformat()}）"
    return f"{d.isoformat()}（{_WD_JP[d.weekday()]}）"


def resolve_date(text: str):
    """日付表現を JST 基準で解決する。(iso, 表示ラベル, エラー理由) を返す.

    日付の記載が無ければ (None, None, None)。
    """
    t = text or ""
    today = today_jst_date()

    def _past(d: date):
        if d > today:
            return None, None, "未来の日付は指定できません"
        return d.isoformat(), _label(d, today), None

    m = _ISO_PAT.search(t)
    if m:
        try:
            return _past(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            return None, None, "日付を読み取れませんでした"

    m = _SLASH_PAT.search(t)
    if m:
        try:
            mo, dd = int(m.group(1)), int(m.group(2))
            d = date(today.year, mo, dd)
            if d > today:
                d = date(today.year - 1, mo, dd)
            return _past(d)
        except ValueError:
            return None, None, "日付を読み取れませんでした"

    if re.search(r"おととい|一昨日|おとつい", t):
        return _past(today - timedelta(days=2))
    if re.search(r"昨日|きのう|前日", t):
        return _past(today - timedelta(days=1))
    if re.search(r"今日|きょう|本日|さっき|たった今|今朝|今晩", t):
        return _past(today)

    m = _DAYS_AGO_PAT.search(t)
    if m:
        n = _kanji_num(m.group(1))
        if n is not None and 0 <= n <= 365:
            return _past(today - timedelta(days=n))

    m = _WD_PAT.search(t)
    if m:
        rel, wd = m.group(1), m.group(2)
        if rel in ("来週", "再来週"):
            return None, None, "未来の日付は指定できません"
        idx = _WD_JP.index(wd)
        d = today - timedelta(days=(today.weekday() - idx) % 7)
        if rel == "先週":
            d -= timedelta(days=7)
        elif rel == "先々週":
            d -= timedelta(days=14)
        return _past(d)

    return None, None, None


def resolve_slot(text: str):
    """食事区分を推定する（特定できなければ None）."""
    t = text or ""
    for pat, val in _SLOT_PATTERNS:
        if re.search(pat, t):
            return val
    return None


def parse_repeat_ref(text: str):
    """ルールベースで (日付, 区分) を抽出する。複製意図でなければ None."""
    if not looks_like_repeat(text):
        return None
    d, label, err = resolve_date(text)
    return {"date": d, "date_label": label, "slot": resolve_slot(text),
            "error": err, "source": "rule"}


def _llm_ref(text: str):
    """ルールで特定できなかったときだけ AI に解釈させる（日付計算はしない）."""
    try:
        from app.services.llm_consult import extract_meal_ref
        out = extract_meal_ref(text)
    except Exception:
        logger.warning("extract_meal_ref failed", exc_info=True)
        return None
    if not isinstance(out, dict) or out.get("is_repeat") is False:
        return None
    return out


def _merge(base: dict, llm: dict) -> dict:
    out = dict(base or {})
    if not out.get("date") and llm.get("date_expr"):
        d, label, err = resolve_date(str(llm["date_expr"]))
        if d:
            out["date"], out["date_label"] = d, label
        elif err:
            out["error"] = err
    if not out.get("slot"):
        meal = str(llm.get("meal") or "").strip()
        out["slot"] = _MEAL_MAP.get(meal) or resolve_slot(meal)
    return out


def fetch_meal(user_id: str, date_iso: str, slot: str):
    """指定日・指定区分の記録をそのまま返す（コピー元）."""
    from app.services.db import fetch_entries_for_date
    try:
        rows = fetch_entries_for_date(user_id, date_iso) or []
    except Exception:
        logger.exception("fetch_meal failed")
        return []
    return [r for r in rows if (r.get("meal_slot") or "") == slot]


def _other_slots(user_id: str, date_iso: str):
    from app.services.db import fetch_entries_for_date
    try:
        rows = fetch_entries_for_date(user_id, date_iso) or []
    except Exception:
        return []
    seen = []
    for r in rows:
        s = r.get("meal_slot") or ""
        if s and s not in seen:
            seen.append(s)
    return [_SLOT_JP.get(s, s) for s in seen]


def _pfc_line(f: dict) -> str:
    def _n(v):
        return f"{float(v):.0f}" if isinstance(v, (int, float)) else "?"
    salt = f.get("salt_g")
    tail = f" 食塩{float(salt):.1f}g" if isinstance(salt, (int, float)) else ""
    return (f" (P{_n(f.get('protein_g'))} F{_n(f.get('fat_g'))}"
            f" C{_n(f.get('carb_g'))}{tail})")


def _confirm(user_id: str, ref: dict, rows: list):
    """コピー内容を提示し、確認ボタンを付けて返す（勝手に保存しない）."""
    from app.handlers.text_handler import _pending
    from app.handlers.button_builder import qr, pb

    slot = ref["slot"]
    foods = []
    for r in rows:
        foods.append({
            "name": r.get("food_name") or "未名",
            "kcal": float(r.get("kcal") or 0),
            "protein_g": r.get("protein_g"), "fat_g": r.get("fat_g"),
            "carb_g": r.get("carb_g"), "salt_g": r.get("salt_g"),
            "quantity_g": r.get("quantity_g"),
            "_source_type": "repeat_copy", "_confidence": "confirmed",
        })
    _pending[user_id] = {
        "foods": foods, "meal_slot": slot, "date": today_jst(),
        "awaiting_slot": False, "created_at": time.time(),
        "source_type": "repeat_copy", "confidence": "confirmed",
        "repeat_from": {"date": ref["date"], "slot": slot},
    }
    total = sum(f["kcal"] for f in foods)
    lines = [f"🔁 {ref['date_label']}の{_SLOT_JP[slot]}と同じ内容を、"
             f"今日の{_SLOT_JP[slot]}として登録します。", ""]
    for f in foods:
        lines.append(f"・{f['name']} {f['kcal']:.0f}kcal" + _pfc_line(f))
    lines += [f"合計 約{total:.0f}kcal（{len(foods)}品）", "",
              "この内容で記録しますか？"]
    msg = TextSendMessage(text="\n".join(lines))
    msg.quick_reply = qr(
        pb("✅ 記録する", "cmd=confirm_yes", "記録する"),
        pb("キャンセル", "cmd=confirm_no", "キャンセル"),
    )
    return msg


def handle_repeat(user_id: str, text: str):
    """複製登録のエントリポイント。TextSendMessage か None を返す."""
    ref = parse_repeat_ref(text)
    if ref is None:
        return None
    if not ref.get("date") or not ref.get("slot"):
        llm_ref = _llm_ref(text)
        if llm_ref:
            ref = _merge(ref, llm_ref)

    if ref.get("error"):
        return TextSendMessage(text=(
            "⚠ " + str(ref["error"]) + "\n"
            "例: 「昨日の昼と同じ」「おとといの夕食と同じ」「10/15の朝と同じ」"))
    if not ref.get("date"):
        return TextSendMessage(text=(
            "いつの食事か教えてください。\n"
            "例: 「昨日の昼と同じ」「おとといの夕食と同じ」"
            "「先週の火曜の昼と同じ」「10/15の朝と同じ」"))
    if not ref.get("slot"):
        return TextSendMessage(text=(
            f"{ref['date_label']}のどの食事ですか？\n"
            f"例: 「{ref['date_label']}の昼と同じ」のように、"
            "朝食／昼食／夕食／間食を入れてください"))

    rows = fetch_meal(user_id, ref["date"], ref["slot"])
    if not rows:
        others = _other_slots(user_id, ref["date"])
        hint = ("\nこの日の記録: " + "、".join(others)) if others else \
               "\nこの日は記録がありません。"
        return TextSendMessage(text=(
            f"{ref['date_label']}の{_SLOT_JP[ref['slot']]}の記録が見つかりませんでした。"
            + hint +
            "\n日付や区分を変えてもう一度送ってください"
            "（例: 「おとといの夕食と同じ」）"))
    return _confirm(user_id, ref, rows)
