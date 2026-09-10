import logging
import re
from datetime import date
from linebot.models import TextSendMessage, FlexSendMessage
from app.services.db import fetch_day_summary, fetch_recent_history, save_entry
from app.services.calorie_calc import parse_record_line
from app.handlers.flex_builder import summary_flex, history_flex

logger = logging.getLogger(__name__)
DATE_PAT = re.compile(r"(?:(\d{1,2})\s*/\s*(\d{1,2}))?")
SLOT_MAP = {"朝":"breakfast","昼":"lunch","夕":"dinner","夜":"dinner","間":"snack"}


def _today():
    return date.today().isoformat()


def _norm_date(m):
    if not m:
        return _today()
    mo_s, d_s = m[0], m[1]
    if mo_s is None or d_s is None:
        return _today()
    mo, d = int(mo_s), int(d_s)
    today = date.today()
    y = today.year if today.month >= mo else today.year - 1
    return f"{y}-{mo:02d}-{d:02d}"


def handle_text(user_id: str, text: str):
    try:
        text = text.strip()
        if not text:
            return TextSendMessage(text="「集計」「履歴」または『朝 食パン100g 200kcal』のように送ってください")

        if text in ("集計","今日","summary","Summary"):
            s = fetch_day_summary(user_id, _today())
            return FlexSendMessage(alt_text=f"{_today()} のサマリ", contents=summary_flex(s))

        if text in ("履歴","history","History","りれき"):
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
            user_id=user_id, date=d,
            meal_slot=parsed["meal_slot"], food_name=parsed["food_name"],
            kcal=parsed["kcal"], protein_g=parsed.get("protein_g"),
            fat_g=parsed.get("fat_g"), carb_g=parsed.get("carb_g"),
            salt_g=parsed.get("salt_g"), quantity_g=parsed.get("quantity_g"),
            source_type="user_report", confidence="estimated",
        )
        return TextSendMessage(text=_format_record(d, parsed))

    except Exception as exc:
        logger.exception("handle_text error")
        return TextSendMessage(text=f"⚠ エラー: {type(exc).__name__}: {str(exc)[:200]}")


def _format_record(d, p):
    return (
        f"✅ 記録: {d} {p['meal_slot']} {p['food_name']}\n"
        f"   {p['kcal']}kcal / P{p.get('protein_g','?')} "
        f"F{p.get('fat_g','?')} C{p.get('carb_g','?')} 食塩{p.get('salt_g','?')}g"
        f"\n   source=user_report / confidence=estimated"
    )


def _format_history(rows):
    if not rows:
        return "履歴がありません"
    lines = ["📊 直近7日（推定ベース）"]
    for r in rows[:7]:
        lines.append(
            f"{r['date']}: 摂取 {r['intake_kcal']:.0f}kcal "
            f"赤字 {r['deficit_kcal']:.0f}kcal"
        )
    return "\n".join(lines)
