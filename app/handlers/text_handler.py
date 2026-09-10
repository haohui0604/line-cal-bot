"""テキスト入力 → commands / records / summary 振り分け."""
import logging
import re
from datetime import date
from linebot.models import TextSendMessage, FlexSendMessage
from app.services.db import save_entry, fetch_day_summary, fetch_recent_history
from app.services.calorie_calc import parse_record_line
from app.handlers.flex_builder import summary_flex

logger = logging.getLogger(__name__)

DATE_PAT = re.compile(r"(?:(\d{1,2})\s*/\s*(\d{1,2}))?")

GREETINGS = {
    "おはよう":         "おはようございます！今日も記録頑張りましょう 🌅",
    "おはようございます": "おはようございます！今日も記録頑張りましょう 🌅",
    "こんにちは":       "こんにちは！お昼の記録どうぞ 🌤️",
    "こんばんは":       "こんばんは！夕食・まとめの記録どうぞ 🌙",
}


def _today() -> str:
    return date.today().isoformat()


def _norm_date(m):
    """DATE_PAT.match().groups() または None を受けて YYYY-MM-DD を返す."""
    if m is None or len(m) < 2 or m[0] is None or m[1] is None:
        return _today()
    mo, d = int(m[0]), int(m[1])
    today = date.today()
    y = today.year if today.month >= mo else today.year - 1
    return f"{y}-{mo:02d}-{d:02d}"


def handle_text(user_id: str, text: str):
    """LINEテキストメッセージの主ハンドラ."""
    try:
        text = text.strip()
        if not text:
            return TextSendMessage(text=(
                "「集計」「履歴」または\n"
                "『朝 食パン100g 250kcal P9 F4 C48 食塩1.5g』のように送ってください"
            ))
        if text in GREETINGS:
            return TextSendMessage(text=GREETINGS[text])
        if text in ("集計", "今日", "summary", "Summary"):
            s = fetch_day_summary(user_id, _today())
            return FlexSendMessage(
                altText=f"{_today()} 集計 {s['intake_kcal']:.0f}kcal",
                contents=summary_flex(s),
            )
        if text in ("履歴", "history", "History", "りれき"):
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
                "受け取り例:\n"
                "  『朝 食パン100g 250kcal P9 F4 C48』\n"
                "  『9/10 昼 ルーローハン81g 食塩2.8g』\n"
                "コマンド: 『集計』『履歴』『グラフ』"
            ))
        kcal = parsed.get("kcal") or 0.0
        if kcal <= 0:
            return TextSendMessage(text=(
                f"栄養情報が不足しています。\n"
                f"食品: {parsed['food_name']} (区分: {parsed['meal_slot']})\n"
                f"次回送信例:\n"
                f"  『{parsed['meal_slot']} {parsed['food_name']}100g 250kcal P9 F4 C48 食塩1.5g』"
            ))
        d = _norm_date(m.groups() if m else None)
        save_entry(
            user_id=user_id, date=d,
            meal_slot=parsed["meal_slot"], food_name=parsed["food_name"],
            kcal=kcal, protein_g=parsed.get("protein_g"),
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
        f"   {p['kcal']:.0f}kcal / P{p.get('protein_g','?')} "
        f"F{p.get('fat_g','?')} C{p.get('carb_g','?')} 食塩{p.get('salt_g','?')}g"
        f"\n   source=user_report / confidence=estimated"
    )


def _format_history(rows):
    if not rows:
        return "履歴がありません"
    lines = ["📊 直近7日"]
    for r in rows[:7]:
        lines.append(
            f"{r['date']}: 摂取 {r['intake_kcal']:.0f}kcal "
            f"赤字 {r['deficit_kcal']:.0f}kcal"
        )
    return "\n".join(lines)
