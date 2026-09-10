"""テキスト入力 → commands / records / summary / LLM chat 振り分け."""
import logging
import re
from datetime import date

from linebot.models import TextSendMessage, FlexSendMessage

from app.services.db import (
    save_entry, save_activity, save_weight, set_goal, get_goal,
    fetch_day_summary, fetch_recent_history, fetch_today_food_names,
    update_entry_kcal, find_entry_candidates,
)
from app.services.calorie_calc import parse_record_line
from app.handlers.flex_builder import (
    summary_flex, weekly_chart_flex, monthly_summary_flex,
)


logger = logging.getLogger(__name__)

DATE_PAT = re.compile(r"(?:(\d{1,2})\s*/\s*(\d{1,2}))?")

# ユーザー設定 (DB 化済み。未設定なら既定値)
TARGET_KCAL_DEFAULT = 1800.0
PROTEIN_TARGET_G = 100.0

# パターン集
GOAL_PAT = re.compile(r"^目標(?:kcal)?\s*[:：]?\s*(\d+)")
WEIGHT_PAT = re.compile(
    r"^体重\s+([0-9]+(?:\.[0-9]+)?)"
    r"(?:\s+(?:体脂肪|F|脂肪)\s*([0-9]+(?:\.[0-9]+)?))?"
    r"(?:\s+(?:筋肉|M)\s*([0-9]+(?:\.[0-9]+)?))?"
    r"(?:\s+(?:BMR|基礎代謝)\s*([0-9]+))?"
)
ACTIVITY_PAT = re.compile(
    r"^(?:運動|活動|消費)\s+([0-9]+)(?:\s*kcal)?"
    r"(?:\s+(?:活動|active|ACTIVE)\s+([0-9]+))?"
    r"(?:\s+(?:安静|resting|RESTING|基礎代謝|basal)\s+([0-9]+))?"
)
# 修正コマンド: 「修正 9/9 昼 牛丼並盛 720kcal」
MODIFY_PAT = re.compile(
    r"^修正\s+(\d{1,2})/(\d{1,2})"
    r"(?:\s+(朝|昼|夜|夜食|breakfast|lunch|dinner|snack))?"
    r"\s+(\S+?)\s+(\d+(?:\.\d+)?)(?:\s*kcal)?$"
)
# 確認フロー用 (複数候補があった場合)
MODIFY_CONFIRM_PAT = re.compile(r"^修正候補\s+(\d+)\s*$")

GREETINGS = {
    "おはよう":         "おはようございます！今日も記録頑張りましょう 🌅",
    "おはようございます": "おはようございます！今日も記録頑張りましょう 🌅",
    "こんにちは":       "こんにちは！お昼の記録どうぞ 🌤️",
    "こんばんは":       "こんばんは！夕食・まとめの記録どうぞ 🌙",
}

# LLM 推定の未確定レコード (Render 再起動で消える簡易実装)
_pending: dict = {}           # user_id -> {"foods": [...], "meal_slot": str}
# 修正コマンドの複数候補確認用
_modify_pending: dict = {}    # user_id -> {"candidates": [...], "new_kcal": float}


def _today() -> str:
    return date.today().isoformat()


def _norm_date(m):
    if m is None or len(m) < 2 or m[0] is None or m[1] is None:
        return _today()
    mo, d = int(m[0]), int(m[1])
    today = date.today()
    y = today.year if today.month >= mo else today.year - 1
    return f"{y}-{mo:02d}-{d:02d}"


def _resolve_target_kcal(user_id: str, iso_date: str) -> float:
    """DB の goals から該当日以下の最新目標を取得。未設定なら既定値."""
    g = get_goal(user_id, iso_date)
    return float(g) if g is not None else TARGET_KCAL_DEFAULT


def _build_context(user_id: str) -> dict:
    s = fetch_day_summary(user_id, _today())
    target = _resolve_target_kcal(user_id, _today())
    remaining = max(target - s["intake_kcal"], 0)
    return {
        "intake_kcal": s["intake_kcal"],
        "burn_kcal": s["burn_kcal"],
        "target_kcal": target,
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


def _slot_to_japanese(slot: str) -> str:
    return {"breakfast": "朝食", "lunch": "昼食",
            "dinner": "夕食", "snack": "間食"}.get(slot, slot)


def _slot_to_english(slot: str) -> str:
    return {"朝": "breakfast", "昼": "lunch",
            "夜": "dinner", "夜食": "snack"}.get(slot, slot)


def handle_text(user_id: str, text: str):
    try:
        text = text.strip()
        if not text:
            return TextSendMessage(text=(
                "「集計」「履歴」「週次」「月次」「目標」のいずれかを入力するか、\n"
                "『朝 食パン100g 250kcal』形式で送ってください。\n"
                "自由文 (例:『さっきラーメン食べた』) もOKです"
            ))

        # 0) LLM 推定の確定/取消
        if user_id in _pending and text in (
            "はい", "うん", "記録", "ok", "OK", "Yes", "YES"
        ):
            p = _pending.pop(user_id)
            _save_foods(user_id, p["foods"], p["meal_slot"])
            names = " / ".join(f.get("name", "?") for f in p["foods"])
            return TextSendMessage(text=(
                f"✅ 記録しました: {names}\n"
                "『集計』で今日の合計を確認できます"
            ))
        if user_id in _pending and text in (
            "いいえ", "やめる", "キャンセル", "ng", "NG", "No", "NO"
        ):
            _pending.pop(user_id)
            return TextSendMessage(text="記録をキャンセルしました")

        # 0.5) 修正コマンドの複数候補からの選択
        if user_id in _modify_pending:
            m = MODIFY_CONFIRM_PAT.match(text)
            if m:
                idx = int(m.group(1)) - 1
                mp = _modify_pending.pop(user_id)
                if 0 <= idx < len(mp["candidates"]):
                    c = mp["candidates"][idx]
                    update_entry_kcal(
                        user_id=user_id,
                        entry_id=c["id"],
                        new_kcal=mp["new_kcal"],
                    )
                    return TextSendMessage(text=(
                        f"✅ 修正しました: {c['date']} {c['meal_slot']} "
                        f"{c['food_name']}\n"
                        f"kcal: {c['kcal']:.0f} → {mp['new_kcal']:.0f}"
                    ))
                return TextSendMessage(text=(
                    f"候補は 1〜{len(mp['candidates'])} です。番号で選んでください"
                ))
            if text in ("いいえ", "やめる", "キャンセル"):
                _modify_pending.pop(user_id)
                return TextSendMessage(text="修正をキャンセルしました")

        # 1) 挨拶
        if text in GREETINGS:
            return TextSendMessage(text=GREETINGS[text])

        # 2) 集計
        if text in ("集計", "今日", "summary", "Summary"):
            s = fetch_day_summary(user_id, _today())
            return FlexSendMessage(
                alt_text=f"{_today()} 集計 {s['intake_kcal']:.0f}kcal",
                contents=summary_flex(s),
            )

        # 3) 履歴 (テキスト形式)
        if text in ("履歴", "history", "History", "りれき"):
            rows = fetch_recent_history(user_id, days=7)
            return TextSendMessage(text=_format_history(rows))

        # 4) 週次 / 月次 / グラフ
        if text in ("週次", "週間", "week", "Week", "グラフ"):
            rows = fetch_recent_history(user_id, days=7)
            tgt = _resolve_target_kcal(user_id, _today())
            return FlexSendMessage(
                alt_text=f"直近7日 レポート (目標 {tgt:.0f}kcal)",
                contents=weekly_chart_flex(rows, days=7, target_kcal=tgt),
            )
        if text in ("月次", "月間", "month", "Month"):
            rows = fetch_recent_history(user_id, days=30)
            tgt = _resolve_target_kcal(user_id, _today())
            contents = [
                monthly_summary_flex(rows, target_kcal=tgt),
                weekly_chart_flex(rows, days=30, target_kcal=tgt),
            ]
            return FlexSendMessage(
                alt_text=f"直近30日 レポート (目標 {tgt:.0f}kcal)",
                contents=contents,
            )

        # 5) 目標摂取カロリー (数字付きで設定 / 数字なしで表示)
        m = GOAL_PAT.match(text)
        if m:
            kcal = float(m.group(1))
            if kcal < 1000 or kcal > 4000:
                return TextSendMessage(text=(
                    f"⚠ {kcal:.0f}kcal は範囲外です。1000〜4000 で指定してください"
                ))
            set_goal(user_id=user_id, date=_today(), target_kcal=kcal)
            s = fetch_day_summary(user_id, _today())
            return TextSendMessage(text=(
                f"🎯 目標摂取カロリーを {kcal:.0f}kcal に設定しました\n"
                f"今日の摂取: {s['intake_kcal']:.0f}kcal / "
                f"残り {max(kcal - s['intake_kcal'], 0):.0f}kcal"
            ))
        if text in ("目標", "目標表示"):
            tgt = _resolve_target_kcal(user_id, _today())
            return TextSendMessage(text=(
                f"今の目標摂取カロリーは {tgt:.0f}kcal です\n"
                "変更: 『目標 1800』のように送ってください"
            ))

        # 6) 体重記録 (文字入力)
        m = WEIGHT_PAT.match(text)
        if m:
            weight_kg = float(m.group(1))
            body_fat = float(m.group(2)) if m.group(2) else None
            muscle = float(m.group(3)) if m.group(3) else None
            bmr = float(m.group(4)) if m.group(4) else None
            save_weight(
                user_id=user_id, date=_today(),
                weight_kg=weight_kg, is_measured=1,
                body_fat_pct=body_fat, muscle_kg=muscle,
                bmr_kcal=bmr, note="text_input",
            )
            extra = []
            if body_fat is not None:
                extra.append(f"体脂肪 {body_fat}%")
            if muscle is not None:
                extra.append(f"筋肉 {muscle}kg")
            if bmr is not None:
                extra.append(f"BMR {bmr}kcal")
            base = f"⚖️ 体重記録: {weight_kg}kg"
            if extra:
                base += " (" + " / ".join(extra) + ")"
            return TextSendMessage(text=base + "\n『履歴』で推移を確認できます")

        # 7) 活動(消費)カロリー
        m = ACTIVITY_PAT.match(text)
        if m:
            total = float(m.group(1))
            active = float(m.group(2)) if m.group(2) else None
            resting = float(m.group(3)) if m.group(3) else None
            save_activity(
                user_id=user_id, date=_today(),
                total_kcal=total, active_kcal=active,
                resting_kcal=resting, source_type="user_report",
                ocr_image_url=None,
            )
            s = fetch_day_summary(user_id, _today())
            tgt = _resolve_target_kcal(user_id, _today())
            extra = ""
            if active or resting:
                extra = f" (活動 {active or 0:.0f} / 安静 {resting or 0:.0f})"
            return TextSendMessage(text=(
                f"🏃 活動記録: {total:.0f}kcal{extra}\n"
                f"今日の消費 {s['burn_kcal']:.0f}kcal / 摂取 {s['intake_kcal']:.0f}kcal\n"
                f"目標 {tgt:.0f}kcal まで残り {max(tgt - s['intake_kcal'], 0):.0f}kcal\n"
                f"赤字 {s['deficit_kcal']:+.0f}kcal"
            ))

        # 8) 過去データ修正
        m = MODIFY_PAT.match(text)
        if m:
            mo, d = int(m.group(1)), int(m.group(2))
            slot_jp = m.group(3)
            food_name = m.group(4)
            new_kcal = float(m.group(5))
            today = date.today()
            y = today.year if today.month >= mo else today.year - 1
            target_date = f"{y}-{mo:02d}-{d:02d}"
            slot_en = _slot_to_english(slot_jp) if slot_jp else None

            if slot_en:
                # meal_slot が指定されている場合は直接 UPDATE を試みる
                updated = update_entry_kcal(
                    user_id=user_id, date=target_date,
                    meal_slot=slot_en, food_name=food_name,
                    new_kcal=new_kcal,
                )
                if updated:
                    return TextSendMessage(text=(
                        f"✅ 修正しました: {target_date} {slot_jp} {food_name}\n"
                        f"kcal: → {new_kcal:.0f}"
                    ))
                # 一致しなかった場合は候補検索へ
                candidates = find_entry_candidates(
                    user_id=user_id, date=target_date,
                    meal_slot=slot_en,
                )
            else:
                # meal_slot 未指定 → その日の全スロットから候補検索
                candidates = find_entry_candidates(
                    user_id=user_id, date=target_date,
                )

            if not candidates:
                return TextSendMessage(text=(
                    f"{target_date} に該当する記録が見つかりませんでした。\n"
                    "『履歴』で確認してから再度指定してください"
                ))
            if len(candidates) == 1:
                c = candidates[0]
                update_entry_kcal(
                    user_id=user_id,
                    entry_id=c["id"],
                    new_kcal=new_kcal,
                )
                return TextSendMessage(text=(
                    f"✅ 修正しました: {c['date']} {c['meal_slot']} "
                    f"{c['food_name']}\n"
                    f"kcal: {c['kcal']:.0f} → {new_kcal:.0f}"
                ))
            # 複数候補 → 確認フロー
            _modify_pending[user_id] = {
                "candidates": candidates,
                "new_kcal": new_kcal,
            }
            lines = [f"{target_date} の記録が複数あります。番号で選んでください:"]
            for i, c in enumerate(candidates, 1):
                lines.append(
                    f"{i}. {c['meal_slot']} {c['food_name']} "
                    f"({c['kcal']:.0f}kcal → {new_kcal:.0f}kcal)"
                )
            lines.append("例: 『修正候補 1』 / キャンセル: 『いいえ』")
            return TextSendMessage(text="\n".join(lines))

        # 9) ルールベース食事登録 (kcal 明記時のみ即保存)
        m = DATE_PAT.match(text)
        body = text[m.end():].strip() if m else text
        parsed = parse_record_line(body)
        if parsed is not None and (parsed.get("kcal") or 0.0) > 0:
            d = _norm_date(m.groups() if m else None)
            kcal = parsed["kcal"]
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
        return TextSendMessage(text=(
            "AI応答に失敗しました。少し待って再送するか、\n"
            "『朝 食パン100g 250kcal』形式で直接記録してください"
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
            f"赤字 {r['deficit_kcal']:+.0f}kcal"
        )
    return "\n".join(lines)
