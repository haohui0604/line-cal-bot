"""テキスト入力 → commands / records / summary / LLM chat 振り分け."""
import json
import logging
import re
from datetime import date
from linebot.models import TextSendMessage, FlexSendMessage
from app.services.db import (
    save_entry, fetch_day_summary, fetch_recent_history, fetch_today_food_names,
)
from app.services.calorie_calc import parse_record_line
from app.handlers.flex_builder import summary_flex

logger = logging.getLogger(__name__)

DATE_PAT = re.compile(r"(?:(\d{1,2})\s*/\s*(\d{1,2}))?")

TARGET_KCAL = 1800.0
PROTEIN_TARGET_G = 100.0

GREETINGS = {
    "おはよう":         "おはようございます！今日も記録頑張りましょう 🌅",
    "おはようございます": "おはようございます！今日も記録頑張りましょう 🌅",
    "こんにちは":       "こんにちは！お昼の記録どうぞ 🌤️",
    "こんばんは":       "こんばんは！夕食・まとめの記録どうぞ 🌙",
}

_pending: dict = {}

def _today() -> str:
    return date.today().isoformat()


def _norm_date(m):
    if m is None or len(m) < 2 or m[0] is None or m[1] is None:
        return _today()
    mo, d = int(m[0]), int(m[1])
    today = date.today()
    y = today.year if today.month >= mo else today.year - 1
    return f"{y}-{mo:02d}-{d:02d}"


def _build_context(user_id: str) -> dict:
    s = fetch_day_summary(user_id, _today())
    remaining = max(TARGET_KCAL - s["intake_kcal"], 0)
    return {
        "intake_kcal": s["intake_kcal"],
        "burn_kcal": s["burn_kcal"],
        "target_kcal": TARGET_KCAL,
        "remaining_kcal": remaining,
        "protein_g": s["protein_g"],
        "protein_target_g": PROTEIN_TARGET_G,
        "remaining_protein_g": max(PROTEIN_TARGET_G - s["protein_g"], 0),
        "salt_g": s["salt_g"],
        "today_foods": fetch_today_food_names(user_id, _today()),
    }


def _save_foods(user_id: str, foods: list, meal_slot: str) -> None:
    for f in foods:
        save_entry(
            user_id=user_id, date=_today(),
            meal_slot=meal_slot or "snack",
            food_name=f.get("name") or "未名",
            kcal=float(f.get("kcal") or 0),
            protein_g=f.get("protein_g"), fat_g=f.get("fat_g"),
            carb_g=f.get("carb_g"), salt_g=f.get("salt_g"),
            quantity_g=f.get("quantity_g"),
            source_type="llm_estimate", confidence="estimated",
        )


def handle_text(user_id: str, text: str):
    try:
        text = text.strip()
        if not text:
            return TextSendMessage(text=(
                "「集計」「履歴」または\n"
                "『朝 食パン100g 250kcal』のように送ってください。\n"
                "自由文 (例:『さっきラーメン食べた』) もOKです"
            ))

        if user_id in _pending and text in ("はい", "うん", "記録", "ok", "OK"):
            p = _pending.pop(user_id)
            _save_foods(user_id, p["foods"], p["meal_slot"])
            names = " / ".join(f.get("name", "?") for f in p["foods"])
            return TextSendMessage(text=(
                f"✅ 記録しました: {names}\n"
                "『集計』で今日の合計を確認できます"
            ))
        if user_id in _pending and text in ("いいえ", "やめる", "キャンセル", "ng", "NG"):
            _pending.pop(user_id)
            return TextSendMessage(text="記録をキャンセルしました")

        if text in GREETINGS:
            return TextSendMessage(text=GREETINGS[text])

        if text in ("集計", "今日", "summary", "Summary"):
            s = fetch_day_summary(user_id, _today())
            return FlexSendMessage(
                alt_text=f"{_today()} 集計 {s['intake_kcal']:.0f}kcal",
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
        if parsed is not None:
            kcal = parsed.get("kcal") or 0.0
            if kcal <= 0:
                return TextSendMessage(text=(
                    f"栄養情報が不足しています。\n"
                    f"食品: {parsed['food_name']} (区分: {parsed['meal_slot']})\n"
                    f"kcalを付けるか、自由文で送るとAIが推定します"
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

        return _handle_llm(user_id, text)

    except Exception as exc:
        logger.exception("handle_text error")
        return TextSendMessage(text=f"⚠ エラー: {type(exc).__name__}: {str(exc)[:200]}")


def _handle_llm(user_id: str, text: str):
    from app.services.llm import chat
    try:
        result = chat(text, _build_context(user_id))
    except Exception as exc:
        logger.exception("LLM call failed")
        safe = str(exc).split("?key=", 1)[0]  # キーが混入していても切る
        return TextSendMessage(text=(
            f"⚠ AIデバッグ: {type(exc).__name__}: {safe[:200]}"
        ))

    intent = result.get("intent", "chat")
    reaction = (result.get("reaction") or "").strip()

    if intent == "record" and result.get("foods"):
        foods = result["foods"]
        slot = result.get("meal_slot") or "snack"
        _pending[user_id] = {"foods": foods, "meal_slot": slot}
        total_kcal = sum(float(f.get("kcal") or 0) for f in foods)
        lines = []
        if reaction:
            lines.append(reaction)
        lines.append("")
        lines.append("AI推定 (記録前の確認):")
        for f in foods:
            lines.append(
                f"・{f.get('name','?')} {float(f.get('kcal') or 0):.0f}kcal"
                f" (P{f.get('protein_g','?')} F{f.get('fat_g','?')}"
                f" C{f.get('carb_g','?')} 食塩{f.get('salt_g','?')}g)"
            )
        lines.append(f"合計 約{total_kcal:.0f}kcal")
        lines.append("")
        lines.append("この内容で記録しますか？ →「はい」/「いいえ」")
        return TextSendMessage(text="\n".join(lines))

    answer = (result.get("answer") or "").strip()
    parts = [p for p in (reaction, answer) if p]
    if not parts:
        parts = ["なるほど！食事の報告は『ラーメン食べた』など自由文でOKです"]
    return TextSendMessage(text="\n".join(parts))


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
