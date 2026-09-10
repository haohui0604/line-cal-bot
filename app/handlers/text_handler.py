"""テキスト入力 → commands / records / summary 振り分け."""
import re
from datetime import date
from linebot.models import TextSendMessage, FlexSendMessage
from app.services.db import save_entry, fetch_day_summary, fetch_recent_history
from app.services.calorie_calc import parse_record_line
from app.handlers.flex_builder import summary_flex, history_flex

DATE_PAT = re.compile(r"(?:(\d{1,2})\s*/\s*(\d{1,2}))?")
SLOT_MAP = {"朝": "breakfast", "昼": "lunch", "夕": "dinner", "夜": "dinner", "間": "snack"}


def _today() -> str:
    return date.today().isoformat()


def _norm_date(m):
    if not m:
        return _today()
    mo, d = int(m[0]), int(m[1])
    today = date.today()
    y = today.year if today.month >= mo else today.year - 1
    return f"{y}-{mo:02d}-{d:02d}"


def handle_text(user_id: str, text: str):
    text = text.strip()
    if text in ("集計", "今日"):
        s = fetch_day_summary(user_id, _today())
        return FlexSendMessage(altText=f"{_today()} 集計", contents=summary_flex(s))

    if text == "履歴":
        rows = fetch_recent_history(user_id, days=7)
        return TextSendMessage(text=_format_history(rows))

    if text == "グラフ":
        return TextSendMessage(text="グラフ機能は chart_gen.py を参照してください")

    m = DATE_PAT.match(text)
    body = text[m.end():].strip() if m else text
    parsed = parse_record_line(body)
    if parsed is None:
        return TextSendMessage(text=(
            "認識できませんでした。\n"
            "例：『9/10 昼 ルーローハン81g 食塩2.8g』 または\n"
            "『集計』『履歴』『グラフ』"
        ))
    d = _norm_date(m.groups() if m else None)
    save_entry(
        user_id=user_id,
        date=d,
        meal_slot=parsed["meal_slot"],
        food_name=parsed["food_name"],
        kcal=parsed["kcal"],
        protein_g=parsed.get("protein_g"),
        fat_g=parsed.get("fat_g"),
        carb_g=parsed.get("carb_g"),
        salt_g=parsed.get("salt_g"),
        quantity_g=parsed.get("quantity_g"),
        source_type="user_report",
        confidence="estimated",
    )
    return TextSendMessage(text=_format_record(d, parsed))


def _format_record(d, p):
    return (
        f"✅ 記録: {d} {p['meal_slot']} {p['food_name']}\n"
        f"   {p['kcal']}kcal / P{p.get('protein_g', '?')} "
        f"F{p.get('fat_g', '?')} C{p.get('carb_g', '?')} 食塩{p.get('salt_g', '?')}g"
        f"\n   source=user_report / confidence=estimated"
    )


def _format_history(rows):
    if not rows:
        return "履歴がありません"
    lines = ["📊 直近7日（推定ベース）"]
    for r in rows:
        lines.append(
            f"{r['date']}: 摂取 {r['intake_kcal']:.0f}kcal "
            f"赤字 {r['deficit_kcal']:.0f}kcal"
        )
    return "\n".join(lines)
