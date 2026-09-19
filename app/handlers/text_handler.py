"""テキスト入力 → commands / records / summary / LLM chat 振り分け."""
import logging
import re
from datetime import timedelta
from app.handlers.flex_builder import daily_flex
from app.handlers.button_builder import qr, pb
from app.services.report_ai import build_daily_facts, generate_daily
from app.services.db import fetch_day_meals, fetch_day_activity_total
from datetime import date

from linebot.models import TextSendMessage, FlexSendMessage

from app.services.db import (
    save_entry, save_activity, save_weight, set_goal, get_goal,
    fetch_day_summary, fetch_recent_history, fetch_today_food_names,
    update_entry_kcal, find_entry_candidates,
    fetch_latest_weight, fetch_weight_series, fetch_entries_for_date,
    load_user_persona, save_user_persona,
    fetch_recent_entries, delete_entry, delete_entries_by_date,
)
from app.services.calorie_calc import parse_record_line
from app.handlers.flex_builder import (
    summary_flex, weekly_chart_flex, monthly_summary_flex,
)

logger = logging.getLogger(__name__)

DATE_PAT = re.compile(r"(?:(\d{1,2})\s*/\s*(\d{1,2}))?")

TARGET_KCAL_DEFAULT = 1800.0
PROTEIN_TARGET_G = 100.0

# ---- パターン集 ----
GOAL_PAT = re.compile(r"^目標(?:kcal)?\s*[:：]?\s*(\d+)")
# 例:「体重 72.5」「1/1 体重 72」「1/1 体重 72.5推定」
WEIGHT_PAT = re.compile(
    r"^(?:(\d{1,2})/(\d{1,2})\s+)?体重\s+([0-9]+(?:\.[0-9]+)?)(推定)?"
    r"(?:\s+(?:体脂肪|F|脂肪)\s*([0-9]+(?:\.[0-9]+)?))?"
    r"(?:\s+(?:筋肉|M)\s*([0-9]+(?:\.[0-9]+)?))?"
    r"(?:\s+(?:BMR|基礎代謝)\s*([0-9]+))?"
)
# 例:「運動 320」「1/1 消費 2200 活動 500 安静 1700」
ACTIVITY_PAT = re.compile(
    r"^(?:(\d{1,2})/(\d{1,2})\s+)?(?:運動|活動|消費)\s+([0-9]+)(?:\s*kcal)?"
    r"(?:\s+(?:活動|active|ACTIVE)\s*([0-9]+))?"
    r"(?:\s+(?:安静|resting|RESTING|基礎代謝|basal)\s*([0-9]+))?"
)
# 例:「修正 1/1 昼 牛丼 720」
MODIFY_PAT = re.compile(
    r"^修正\s+(\d{1,2})/(\d{1,2})"
    r"(?:\s+(朝食|昼食|夕食|夜食|間食|朝|昼|夜|夕|間))?"
    r"\s+(\S+?)\s+(\d+(?:\.\d+)?)(?:\s*kcal)?\s*$",
    re.IGNORECASE,
)
MODIFY_CONFIRM_PAT = re.compile(r"^修正候補\s+(\d+)\s*$")

# 記録単位の削除
DELETE_PAT      = re.compile(r"^削除\s+(\d+)\s*$")
DELETE_DATE_PAT = re.compile(r"^削除\s+(\d{1,2})/(\d{1,2})\s*$")

# 例:「牛丼 1/1 昼 720kcal」
TRAILING_DATE_SLOT_PAT = re.compile(
    r"^(?P<food>.+?)\s+"
    r"(?P<mo>\d{1,2})/(?P<d>\d{1,2})\s+"
    r"(?P<slot>朝食|昼食|夕食|夜食|間食|朝|昼|夜|夕|間)\s+"
    r"(?P<kcal>\d+(?:\.\d+)?)\s*kcal\s*$",
    re.IGNORECASE,
)

TRAILING_DATE_PAT = re.compile(
    r"^(?P<food>.+?)\s+"
    r"(?P<mo>\d{1,2})/(?P<d>\d{1,2})\s+"
    r"(?P<kcal>\d+(?:\.\d+)?)\s*kcal\s*$",
    re.IGNORECASE,
)

TRAILING_DATE_PAT = re.compile(
    r"^(?P<food>.+?)\s+"
    r"(?P<mo>\d{1,2})/(?P<d>\d{1,2})\s+"
    r"(?P<kcal>\d+(?:\.\d+)?)\s*kcal\s*$",
    re.IGNORECASE,
)

# ↓↓↓ この直後に追加 ↓↓↓

# 「今日のレポート」「日次」「集計」「今日」+ 日付指定
# （9/18の今日のレポート / 昨日の今日のレポート）
DAILY_PAT = re.compile(
    r"^(?:(\d{1,2})[/月](\d{1,2})日?の?)?(昨日の)?"
    r"(今日のレポート|今日|日次|集計)\s*$"
)


def _parse_daily_date(m):
    """日付指定を解釈。指定なし=今日、昨日の=昨日、M/D=今年（未来なら前年）。"""
    today = _today()
    if m.group(3):
        return today - timedelta(days=1)
    if m.group(1) and m.group(2):
        y = today.year
        d = date(y, int(m.group(1)), int(m.group(2)))
        return d if d <= today else date(y - 1, int(m.group(1)), int(m.group(2)))
    return today


GREETINGS = {
    "おはよう":         "おはようございます！今日も記録頑張りましょう 🌅",
    "おはようございます": "おはようございます！今日も記録頑張りましょう 🌅",
    "こんにちは":       "こんにちは！お昼の記録どうぞ 🌤️",
    "こんばんは":       "こんばんは！夕食・まとめの記録どうぞ 🌙",
}

HELP_TEXT = (
    "📖 使い方ガイド\n"
    "\n"
    "【初期設定】\n"
    "・『初期設定』→ 目的と体重から目標kcalを自動設定\n"
    "・『人格設定』→ AIの名前・性格・一人称を自由にカスタマイズ\n"
    "\n"
    "【記録】\n"
    "・『朝 食パン100g 250kcal』→ 即記録\n"
    "・『ラーメン食べた』→ AI推定(確認あり)\n"
    "・写真送信 → 料理/成分表/体重計/消費kcalを読取\n"
    "・『間食 プロテインバー 180kcal』→ 間食区分\n"
    "\n"
    "【過去データ】\n"
    "・『一括』→ 複数行貼付け →『確定』\n"
    "・『修正 1/1 昼 牛丼 720kcal』→ 過去記録の修正\n"
    "・『1/1 消費 2200』『1/1 体重 72』→ 日付指定可\n"
    "\n"
    "【体組成・運動】\n"
    "・『体重 72 体脂肪18 筋肉52 BMR1500』\n"
    "・『体重』→ 直近7日の推移（未入力日は直前値で表示）\n"
    "\n"
    "【確認・整理】\n"
    "・『集計』『履歴』『週次』『月次』\n"
    "・『削除 3』→ 履歴の番号で1件削除\n"
    "・『削除 1/1』→ その日の食事を全削除\n"
    "・『目標 1800』→ 目標設定 /『目標』→ 確認\n"
    "\n"
    "【相談】\n"
    "・『あと何kcal食べていい？』など自由文でAI相談"
)

SETUP_PURPOSES = {
    "1": "減量", "減量": "減量", "痩せたい": "減量", "やせたい": "減量",
    "2": "維持", "維持": "維持", "現状維持": "維持",
    "3": "増量", "増量": "増量", "筋肉": "増量",
}

_pending: dict = {}
_modify_pending: dict = {}
_bulk_pending: dict = {}
_setup_pending: dict = {}
_persona_pending: dict = {}
_history_index: dict = {}   # user_id -> {番号: レコード}
_delete_pending: dict = {}  # user_id -> 削除待ちレコード

_PERSONA_STEPS = [
    ("bot_name",
     "Q1. AIの名前を教えてください（自由記述・12文字以内）\n"
     "例: ここちゃん / パートナー / 執事くん\n"
     "希望がなければ『デフォルト』で「アシスタント」になります"),
    ("bot_tone",
     "Q2. 性格・口調を自由に書いてください\n"
     "例: 励まし強め / 統計重視で淡々と / ゆるふわ系 / 塩分に厳しめ\n"
     "希望がなければ『デフォルト』でOK"),
    ("bot_pronoun",
     "Q3. 一人称を自由に書いてください\n"
     "例: わたし / ボク / オレ / わたくし\n"
     "希望がなければ『デフォルト』で「わたし」になります"),
]

_PERSONA_DEFAULT_MAP = {
    "bot_name": "アシスタント",
    "bot_tone": "",
    "bot_pronoun": "わたし",
}

_SLOT_JP = {"breakfast": "朝", "lunch": "昼", "dinner": "夜", "snack": "間食"}


def _slot_jp(slot: str) -> str:
    return _SLOT_JP.get(slot, slot or "?")

from datetime import date, timedelta

def _parse_daily_date(m):
    today = date.today()

    if m.group(3):
        return today - timedelta(days=1)

    if m.group(1) and m.group(2):
        y = today.year
        d = date(y, int(m.group(1)), int(m.group(2)))

        if d > today:
            d = date(
                y - 1,
                int(m.group(1)),
                int(m.group(2))
            )

        return d

    return today


def _norm_date(m):
    if m is None or len(m) < 2 or m[0] is None or m[1] is None:
        return _today()
    mo, d = int(m[0]), int(m[1])
    today = date.today()
    y = today.year if today.month >= mo else today.year - 1
    return f"{y}-{mo:02d}-{d:02d}"


def _resolve_target_kcal(user_id: str, iso_date: str) -> float:
    g = get_goal(user_id, iso_date)
    return float(g) if g is not None else TARGET_KCAL_DEFAULT


def _build_context(user_id: str) -> dict:
    s = fetch_day_summary(user_id, _today())
    target = _resolve_target_kcal(user_id, _today())
    remaining = max(target - s["intake_kcal"], 0)
    lw = fetch_latest_weight(user_id, _today())
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
        "weight_kg": lw["weight_kg"] if lw else None,
    }


def _save_foods(user_id: str, foods: list, meal_slot: str,
                rec_date: str = None) -> None:
    d = rec_date or _today()
    for f in foods:
        save_entry(
            user_id=user_id, date=d,
            meal_slot=meal_slot or "snack",
            food_name=f.get("name") or "未名",
            kcal=float(f.get("kcal") or 0),
            protein_g=f.get("protein_g"), fat_g=f.get("fat_g"),
            carb_g=f.get("carb_g"), salt_g=f.get("salt_g"),
            quantity_g=f.get("quantity_g"),
            source_type="llm_estimate", confidence="estimated",
        )


def _slot_to_english(slot: str) -> str:
    return {
        "朝食": "breakfast", "朝": "breakfast",
        "昼食": "lunch", "昼": "lunch",
        "夕食": "dinner", "夜": "dinner", "夕": "dinner",
        "夜食": "snack", "間食": "snack", "間": "snack",
    }.get(slot, slot)


def _get_comment(user_id: str, kind: str, facts: dict) -> str:
    """AIコメントのみ取得（失敗時は空文字）."""
    try:
        from app.services.llm import quick_comment
        return quick_comment(kind, facts, load_user_persona(user_id))
    except Exception:
        logger.exception("comment failed")
        return ""


def _with_comment(user_id: str, kind: str, facts: dict,
                  template_text: str) -> str:
    """AIコメント ＋ 確定値の定型文（二層構造）."""
    c = _get_comment(user_id, kind, facts)
    return f"{c}\n\n{template_text}" if c else template_text


def _finish_setup(user_id: str, data: dict, months):
    purpose = data["purpose"]
    weight = data["weight"]
    goal_w = data.get("goal_weight")
    maintenance = weight * 33.0
    target = maintenance
    warn = []
    if purpose == "減量":
        if goal_w is not None and months and goal_w < weight:
            delta = weight - goal_w
            deficit_day = delta * 7200.0 / (months * 30.0)
            target = maintenance - deficit_day
            if deficit_day > 1000:
                warn.append(
                    "⚠ 目標ペースが急激です（1日1000kcal超の赤字）。"
                    "期間を延ばすことを推奨します")
        else:
            target = maintenance - 500
            warn.append("目標体重・期間が未設定のため、緩やかな -500kcal/日で設定しました")
    elif purpose == "維持":
        target = maintenance
        if goal_w is not None and goal_w < weight - 0.5:
            warn.append(
                "⚠ 「維持」を選択しましたが目標体重は現在より低い設定です。"
                "減量目的に切り替えますか？ →『初期設定』をやり直し")
    elif purpose == "増量":
        target = maintenance + 300
    target = max(1200.0, min(target, 4000.0))
    set_goal(user_id=user_id, date=_today(), target_kcal=target)

    lines = ["🎯 初期設定が完了しました！", "",
             f"目的: {purpose}",
             f"現体重: {weight}kg",
             f"維持カロリー(推定): {maintenance:.0f}kcal/日",
             f"→ 目標摂取カロリー: {target:.0f}kcal/日"]
    if goal_w is not None:
        lines.append(f"目標体重: {goal_w}kg")
    if months:
        lines.append(f"期間: {months}ヶ月")
    lines += ["", *warn,
              "※維持カロリーは簡易推定（体重×33）です。"
              "毎日『消費 ○○○○』を記録すると赤字計算の精度が上がります",
              "変更はいつでも『目標 数値』『初期設定』でできます",
              "AIの名前や性格は『人格設定』でカスタマイズできます"]
    return TextSendMessage(text="\n".join(lines))


def handle_text(user_id: str, text: str):
    try:
        text = text.strip()
        if not text:
            return TextSendMessage(text=(
                "「集計」「履歴」「週次」「月次」「目標」のいずれかを入力するか、\n"
                "『朝 食パン100g 250kcal』形式で送ってください。\n"
                "自由文 (例:『さっきラーメン食べた』) もOKです\n"
                "『使い方』で全機能を確認できます"
            ))

        # 0.05) 削除の確認応答
        if user_id in _delete_pending:
            if text in ("はい", "うん", "ok", "OK", "Yes", "YES"):
                r = _delete_pending.pop(user_id)
                if delete_entry(user_id=user_id, entry_id=r["id"]):
                    label = (f"{r['date']} {_slot_jp(r['meal_slot'])} "
                             f"{r['food_name']}")
                    return TextSendMessage(text=_with_comment(
                        user_id, "delete",
                        {"削除した記録": label},
                        f"🗑 削除しました: {label}"))
                return TextSendMessage(text=(
                    "⚠ 削除できませんでした（既に削除済みかもしれません）"))
            if text in ("いいえ", "やめる", "キャンセル"):
                _delete_pending.pop(user_id)
                return TextSendMessage(text="削除をキャンセルしました")

        # 0) LLM 推定の確定/取消
        if user_id in _pending and text in (
            "はい", "うん", "記録", "ok", "OK", "Yes", "YES"
        ):
            p = _pending.pop(user_id)
            _save_foods(user_id, p["foods"], p["meal_slot"],
                        rec_date=p.get("date"))
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

        if (user_id in _pending
                and text not in ("確認", "確定", "キャンセル", "はい", "いいえ")):
            _pending.pop(user_id, None)

        # 0.5) 修正候補の番号選択
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

        # 0.53) 人格設定ウィザード（回答処理）
        if user_id in _persona_pending:
            if text in ("やめる", "キャンセル"):
                _persona_pending.pop(user_id)
                return TextSendMessage(text=(
                    "人格設定を中止しました。いつでも『人格設定』でやり直せます"
                ))
            st = _persona_pending[user_id]
            key, _prompt = _PERSONA_STEPS[st["step"] - 1]
            if text in ("デフォルト", "なし", "スキップ"):
                val = _PERSONA_DEFAULT_MAP[key]
            else:
                val = text.strip()
            st["data"][key] = val
            st["step"] += 1
            if st["step"] <= len(_PERSONA_STEPS):
                return TextSendMessage(text=_PERSONA_STEPS[st["step"] - 1][1])
            d = st["data"]
            save_user_persona(
                user_id=user_id,
                bot_name=d["bot_name"][:12],
                bot_tone=d["bot_tone"][:100],
                bot_pronoun=d["bot_pronoun"][:6],
            )
            _persona_pending.pop(user_id)
            tone_disp = d["bot_tone"] or "デフォルト（明るく前向き）"
            return TextSendMessage(text=(
                "✨ 人格設定が完了しました！\n\n"
                f"名前: {d['bot_name']}\n"
                f"性格: {tone_disp}\n"
                f"一人称: {d['bot_pronoun']}\n\n"
                "『あなたは誰？』と聞くと名乗ります。\n"
                "変更はいつでも『人格設定』でできます"
            ))

        # 0.54) 人格設定の開始
        if text in ("人格設定", "キャラ設定", "性格設定"):
            cur = load_user_persona(user_id)
            tone_cur = cur["bot_tone"] or "デフォルト"
            _persona_pending[user_id] = {"step": 1, "data": {}}
            return TextSendMessage(text=(
                "🎭 人格設定を始めます（いつでも『やめる』で中止）\n"
                f"現在: 名前={cur['bot_name']} / 一人称={cur['bot_pronoun']}"
                f" / 性格={tone_cur}\n\n"
                + _PERSONA_STEPS[0][1]
            ))

        # 0.55) 初期設定ウィザード
        if text in ("初期設定", "セットアップ"):
            _setup_pending[user_id] = {"step": 1, "data": {}}
            return TextSendMessage(text=(
                "🛠 初期設定を始めます（いつでも『やめる』で中止）\n\n"
                "Q1. 目的を選んでください\n"
                "1: 減量したい\n"
                "2: 現状維持\n"
                "3: 増量・筋肉をつけたい\n"
                "→ 番号か言葉で回答"
            ))

        if user_id in _setup_pending:
            st = _setup_pending[user_id]
            if text in ("やめる", "キャンセル"):
                _setup_pending.pop(user_id)
                return TextSendMessage(text=(
                    "初期設定を中止しました。いつでも『初期設定』で再開できます"
                ))

            if st["step"] == 1:
                purpose = SETUP_PURPOSES.get(text)
                if not purpose:
                    return TextSendMessage(text=(
                        "1 / 2 / 3 の番号、または『減量』『維持』『増量』で回答してください"
                    ))
                st["data"]["purpose"] = purpose
                st["step"] = 2
                return TextSendMessage(text=(
                    f"目的は「{purpose}」ですね。\n\n"
                    "Q2. 現在の体重は？（例: 70.5）"
                ))

            if st["step"] == 2:
                m2 = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:kg)?$", text)
                if not m2:
                    return TextSendMessage(text=(
                        "体重を数値で入力してください（例: 70.5）"
                    ))
                w = float(m2.group(1))
                if w < 30 or w > 250:
                    return TextSendMessage(text="30〜250の範囲で入力してください")
                st["data"]["weight"] = w
                save_weight(user_id=user_id, date=_today(), weight_kg=w,
                            is_measured=1, note="initial_setup")
                st["step"] = 3
                return TextSendMessage(text=(
                    f"現体重 {w}kg を記録しました。\n\n"
                    "Q3. 目標体重は？（例: 65）\n"
                    "決まっていなければ『スキップ』"
                ))

            if st["step"] == 3:
                if text in ("スキップ", "なし", "未定"):
                    st["data"]["goal_weight"] = None
                else:
                    m3 = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:kg)?$", text)
                    if not m3:
                        return TextSendMessage(text=(
                            "目標体重を数値で、または『スキップ』で回答してください"
                        ))
                    gw = float(m3.group(1))
                    if gw < 30 or gw > 250:
                        return TextSendMessage(text="30〜250の範囲で入力してください")
                    st["data"]["goal_weight"] = gw
                st["step"] = 4
                return TextSendMessage(text=(
                    "Q4. 目標達成までの期間は？（例: 3 → 3ヶ月）\n"
                    "『スキップ』でもOK"
                ))

            if st["step"] == 4:
                months = None
                if text not in ("スキップ", "なし", "未定"):
                    m4 = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:ヶ月|か月|ヵ月)?$", text)
                    if not m4:
                        return TextSendMessage(text=(
                            "期間を数値（ヶ月）で、または『スキップ』で回答してください"
                        ))
                    months = float(m4.group(1))
                    if months <= 0 or months > 36:
                        return TextSendMessage(text="0.5〜36ヶ月の範囲で入力してください")
                _setup_pending.pop(user_id)
                return _finish_setup(user_id, st["data"], months)

        # 0.6) 一括登録モード
        if text in ("一括", "一括登録", "import", "Import"):
            if user_id in _bulk_pending and _bulk_pending[user_id]["rows"]:
                n = len(_bulk_pending[user_id]["rows"])
                return TextSendMessage(text=(
                    f"📥 一括登録モード中です（読込済み {n}件）\n"
                    "続きの行を貼り付けるか、『確定』『キャンセル』で終了してください"
                ))
            _bulk_pending[user_id] = {"rows": []}
            return TextSendMessage(text=(
                "📥 一括登録モードです。1行1件で貼り付けてください\n"
                "例:\n"
                "1/1 朝 食パン 250kcal\n"
                "1/1 昼 牛丼 700kcal\n"
                "1/1 夜 鶏むね 150g\n\n"
                "※ kcalが無い行はAIが推定して登録します\n"
                "何度でも追送OK。終わったら「確定」、やめるときは「キャンセル」"
            ))

        if user_id in _bulk_pending:
            if text in ("キャンセル", "やめる"):
                _bulk_pending.pop(user_id)
                return TextSendMessage(text="一括登録をキャンセルしました")
            if text in ("確定", "はい"):
                rows = _bulk_pending.pop(user_id)["rows"]
                if not rows:
                    return TextSendMessage(text="登録対象がありませんでした")

                need_estimate = [r for r in rows
                                 if (r.get("kcal") or 0.0) <= 0]
                already_known = [r for r in rows
                                 if (r.get("kcal") or 0.0) > 0]

                if need_estimate:
                    try:
                        from app.services.llm import estimate_foods_batch
                        need_estimate = estimate_foods_batch(need_estimate)
                    except Exception:
                        logger.exception("batch estimate failed, "
                                         "falling back to per-item")
                        from app.services.llm import estimate_food_single
                        survived = []
                        for it in need_estimate:
                            try:
                                survived.append(estimate_food_single(it))
                            except Exception:
                                logger.exception(
                                    "per-item estimate failed: %s",
                                    it.get("food_name"))
                        need_estimate = survived

                savable = already_known + [r for r in need_estimate
                                           if (r.get("kcal") or 0.0) > 0]
                failed = [r for r in rows if r not in savable]

                saved_count = 0
                for r in savable:
                    save_entry(
                        user_id=user_id, date=r["date"],
                        meal_slot=r["meal_slot"], food_name=r["food_name"],
                        kcal=r["kcal"], protein_g=r.get("protein_g"),
                        fat_g=r.get("fat_g"), carb_g=r.get("carb_g"),
                        salt_g=r.get("salt_g"),
                        quantity_g=r.get("quantity_g"),
                        source_type="bulk_import", confidence="estimated",
                    )
                    saved_count += 1

                ai_ok = sum(1 for r in savable if r not in already_known)
                manual = len(already_known)

                from collections import Counter
                cnt = Counter(r["date"] for r in savable)
                breakdown = " / ".join(
                    f"{d}:{n}件" for d, n in sorted(cnt.items()))

                if saved_count == 0:
                    lines = ["⚠ 1件も登録できませんでした"]
                    if manual == 0 and need_estimate:
                        lines.append("AI推定が全て失敗しました (Gemini混雑)")
                    if failed:
                        lines.append("該当行にkcalを明記して再送してください:")
                        for r in failed[:5]:
                            lines.append(
                                f"・{r['date']} {r['meal_slot']} "
                                f"{r['food_name']}")
                    return TextSendMessage(text="\n".join(lines))

                msg = f"✅ {saved_count}件登録 ({breakdown})\n"
                msg += f"   内訳: kcal明記 {manual}件 / AI推定 {ai_ok}件"
                if failed:
                    msg += (
                        f"\n⚠ 推定失敗でスキップ {len(failed)}件:\n"
                        + "\n".join(
                            f"・{r['date']} {r['meal_slot']} {r['food_name']}"
                            for r in failed[:5])
                    )
                    if len(failed) > 5:
                        msg += f"  …他 {len(failed) - 5}件"
                return TextSendMessage(text=msg)

            added, skipped = [], []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                m2 = DATE_PAT.match(line)
                body2 = line[m2.end():].strip() if m2 else line
                p2 = parse_record_line(body2)
                if p2 is not None:
                    p2["date"] = _norm_date(m2.groups() if m2 else None)
                    added.append(p2)
                else:
                    skipped.append(line)
            _bulk_pending[user_id]["rows"].extend(added)
            no_kcal = sum(1 for r in added if (r.get("kcal") or 0.0) <= 0)
            msg = (f"📥 読み取り: {len(added)}件"
                   f"（累計 {len(_bulk_pending[user_id]['rows'])}件")
            if no_kcal:
                msg += f"、うち{no_kcal}件はAI推定予定"
            msg += "）"
            if skipped:
                msg += (f"\n⚠ 解釈不能でスキップ{len(skipped)}件:\n"
                        + "\n".join(f"・{s[:30]}" for s in skipped[:5]))
            msg += "\n追加するか「確定」で登録"
            return TextSendMessage(text=msg)

        # 1) 挨拶
        if text in GREETINGS:
            return TextSendMessage(text=GREETINGS[text])

        # 1.5) 使い方ガイド
        if text in ("使い方", "使い方案内", "ヘルプ", "help", "Help", "HELP"):
            return TextSendMessage(text=HELP_TEXT)

        # 2) 今日のレポート（旧「集計」。日付指定可）
        m = DAILY_PAT.match(text)
        if m:
            d = _parse_daily_date(m)
            label = d if isinstance(d, str) else d.strftime("%Y-%m-%d")
            s = fetch_day_summary(user_id, label)
            meals = fetch_day_meals(user_id, label)
            burn = fetch_day_activity_total(user_id, label)
            target = _resolve_target_kcal(user_id, label)

            facts = build_daily_facts(
                date_label=label,
                intake=s["intake_kcal"], target=target, burn=burn,
                meals=[f"{k}:{v['food_name']}" for k, v in meals.items()],
            )
            comment = generate_daily(
                facts, persona=load_user_persona(user_id),
                user_id=user_id, period_key=label,
            )
            flex = FlexSendMessage(
                alt_text=f"{label} 今日のレポート",
                contents=daily_flex(label, s, meals, burn, target, comment),
            )
            flex.quick_reply = qr(
                pb("今週のレポート", "cmd=weekly"),
                pb("今月のレポート", "cmd=monthly"),
                pb("履歴", "cmd=history"),
            )
            return flex


        # 3) 履歴 — 記録単位で一覧（番号付き／そのまま削除できる）
        if text in ("履歴", "history", "History", "りれき"):
            rows = fetch_recent_entries(user_id, limit=20)
            if not rows:
                return TextSendMessage(text=(
                    "まだ記録がありません。\n"
                    "『朝 食パン100g 250kcal』や写真で記録できます"))
            _history_index[user_id] = {i: r for i, r in enumerate(rows, 1)}
            lines = ["📋 最近の記録（新しい順）"]
            for i, r in enumerate(rows, 1):
                lines.append(
                    f"{i}. {r['date'][5:]} {_slot_jp(r['meal_slot'])} "
                    f"{r['food_name']} {r['kcal']:.0f}kcal"
                )
            lines += ["", "削除: 『削除 3』（番号指定）",
                      "その日ごと削除: 『削除 1/1』"]
            return TextSendMessage(text="\n".join(lines))

        # 3.2) 番号指定で削除（確認フロー）
        m = DELETE_PAT.match(text)
        if m:
            n = int(m.group(1))
            r = _history_index.get(user_id, {}).get(n)
            if r is None:
                return TextSendMessage(text=(
                    "番号を特定できませんでした。先に『履歴』を送り、"
                    "表示された番号で『削除 3』と指定してください"))
            _delete_pending[user_id] = r
            return TextSendMessage(text=(
                "次の記録を削除しますか？\n"
                f"・{r['date']} {_slot_jp(r['meal_slot'])} "
                f"{r['food_name']} {r['kcal']:.0f}kcal\n"
                "→「はい」/「いいえ」"
            ))

        # 3.3) 日付ごとの一括削除
        m = DELETE_DATE_PAT.match(text)
        if m:
            d = _norm_date((m.group(1), m.group(2)))
            cnt = delete_entries_by_date(user_id=user_id, date=d)
            if cnt:
                return TextSendMessage(text=_with_comment(
                    user_id, "delete",
                    {"削除した日付": d, "削除件数": cnt},
                    f"🗑 {d} の記録 {cnt}件を削除しました"))
            return TextSendMessage(text=f"{d} に記録はありませんでした")

        # 3.5) 体重推移
        if text in ("体重", "体重履歴", "体重推移"):
            rows = fetch_weight_series(user_id, days=7)
            if not rows:
                return TextSendMessage(text=(
                    "体重の記録がまだありません。\n"
                    "『体重 72.5』または体重計の写真で記録できます"
                ))
            lines = ["⚖️ 体重（直近7日）"]
            for r in rows:
                tag = "実測" if r["is_measured"] else "推定(繰越)"
                lines.append(f"{r['date'][5:]}: {r['weight_kg']:.1f}kg ({tag})")
            first, lastw = rows[0]["weight_kg"], rows[-1]["weight_kg"]
            diff = lastw - first
            lines.append(f"7日間の変化: {diff:+.1f}kg")
            return TextSendMessage(text="\n".join(lines))

        # 4) 週次 / 月次 / グラフ
        if text in ("今週のレポート", "今週", "週次", "週間", "week", "Week", "グラフ"):
            rows = fetch_recent_history(user_id, days=7)
            tgt = _resolve_target_kcal(user_id, _today())
            return FlexSendMessage(
                alt_text="直近7日 レポート",
                contents=weekly_chart_flex(rows, days=7, target_kcal=tgt),
            )
        if text in ("今月のレポート", "今月", "月次", "月間", "month", "Month"):
            rows = fetch_recent_history(user_id, days=30)
            tgt = _resolve_target_kcal(user_id, _today())
            return FlexSendMessage(
                alt_text="直近30日 レポート",
                contents=monthly_summary_flex(rows, target_kcal=tgt),
            )

        # 5) 目標摂取カロリー
        m = GOAL_PAT.match(text)
        if m:
            kcal = float(m.group(1))
            if kcal < 1000 or kcal > 4000:
                return TextSendMessage(text=(
                    "⚠ 範囲外です。1000〜4000 kcal で指定してください"
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
                "変更: 『目標 1800』のように送ってください\n"
                "自動計算: 『初期設定』から目的と体重を入力"
            ))

        # 6) 体重記録
        m = WEIGHT_PAT.match(text)
        if m:
            rec_date = _norm_date((m.group(1), m.group(2))) if m.group(1) else _today()
            weight_kg = float(m.group(3))
            is_measured = 0 if m.group(4) else 1
            body_fat = float(m.group(5)) if m.group(5) else None
            muscle = float(m.group(6)) if m.group(6) else None
            bmr = float(m.group(7)) if m.group(7) else None
            save_weight(
                user_id=user_id, date=rec_date,
                weight_kg=weight_kg, is_measured=is_measured,
                body_fat_pct=body_fat, muscle_kg=muscle,
                bmr_kcal=bmr,
                note="backfill" if m.group(1) else "text_input",
            )
            tag = "推定" if is_measured == 0 else "実測"
            extra = []
            if body_fat is not None:
                extra.append(f"体脂肪 {body_fat}%")
            if muscle is not None:
                extra.append(f"筋肉 {muscle}kg")
            if bmr is not None:
                extra.append(f"BMR {bmr}kcal")
            base = f"⚖️ 体重記録: {rec_date} {weight_kg}kg（{tag}）"
            if extra:
                base += " (" + " / ".join(extra) + ")"
            base += "\n『体重』で推移を確認できます"
            return TextSendMessage(text=_with_comment(
                user_id, "weight",
                {"日付": rec_date, "体重(kg)": weight_kg,
                 "種別": tag, "体脂肪(%)": body_fat, "筋肉(kg)": muscle},
                base))

        # 7) 活動(消費)カロリー
        m = ACTIVITY_PAT.match(text)
        if m:
            rec_date = _norm_date((m.group(1), m.group(2))) if m.group(1) else _today()
            total = float(m.group(3))
            active = float(m.group(4)) if m.group(4) else None
            resting = float(m.group(5)) if m.group(5) else None
            save_activity(
                user_id=user_id, date=rec_date,
                total_kcal=total, active_kcal=active,
                resting_kcal=resting, source_type="user_report",
                ocr_image_url=None,
            )
            s = fetch_day_summary(user_id, rec_date)
            extra = ""
            if active or resting:
                extra = f" (活動 {active or 0:.0f} / 安静 {resting or 0:.0f})"
            template = (
                f"🏃 活動記録: {rec_date} {total:.0f}kcal{extra}\n"
                f"消費 {s['burn_kcal']:.0f}kcal / 摂取 {s['intake_kcal']:.0f}kcal\n"
                f"収支 {s['deficit_kcal']:+.0f}kcal"
            )
            return TextSendMessage(text=_with_comment(
                user_id, "activity",
                {"日付": rec_date, "消費(kcal)": f"{total:.0f}",
                 "摂取(kcal)": f"{s['intake_kcal']:.0f}",
                 "収支(kcal)": f"{s['deficit_kcal']:+.0f}"},
                template))

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

            candidates = find_entry_candidates(
                user_id=user_id, date=target_date,
                meal_slot=slot_en, food_name_like=food_name,
            ) if slot_en else []
            if not candidates:
                candidates = find_entry_candidates(
                    user_id=user_id, date=target_date,
                    food_name_like=food_name,
                )
            if not candidates:
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

        # 9) 末尾日付付きの食事記録
        m = TRAILING_DATE_SLOT_PAT.match(text) or TRAILING_DATE_PAT.match(text)
        if m:
            food = m.group("food").strip()
            mo = int(m.group("mo")); d = int(m.group("d"))
            kcal = float(m.group("kcal"))
            slot_en = _slot_to_english(m.group("slot")) if m.groupdict().get("slot") else None
            today = date.today()
            y = today.year if today.month >= mo else today.year - 1
            target_date = f"{y}-{mo:02d}-{d:02d}"
            foods = [{"name": food, "kcal": kcal,
                      "protein_g": None, "fat_g": None,
                      "carb_g": None, "salt_g": None,
                      "quantity_g": None}]
            _pending[user_id] = {
                "foods": foods,
                "meal_slot": slot_en or "snack",
                "date": target_date,
            }
            return TextSendMessage(text=(
                f"{food} {kcal:.0f}kcal を {target_date} の"
                f" {slot_en or 'snack'} として記録しますか？\n"
                "→「はい」/「いいえ」"
            ))

        # 10) ルールベース食事登録
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
            tpl = _format_record(d, parsed)
            return TextSendMessage(text=_with_comment(
                user_id, "record",
                {"日付": d, "食品": parsed["food_name"],
                 "kcal": f"{kcal:.0f}",
                 "タンパク質(g)": parsed.get("protein_g")},
                tpl))

        return _handle_llm(user_id, text)

    except Exception as exc:
        logger.exception("handle_text error")
        return TextSendMessage(text=f"⚠ エラー: {type(exc).__name__}: {str(exc)[:200]}")


def _detect_ref_date(text: str) -> str:
    from datetime import timedelta
    m = re.search(r"(\d{1,2})[/月](\d{1,2})日?", text)
    if m:
        return _norm_date((m.group(1), m.group(2)))
    if "おととい" in text or "一昨日" in text:
        return (date.today() - timedelta(days=2)).isoformat()
    if "昨日" in text or "きのう" in text:
        return (date.today() - timedelta(days=1)).isoformat()
    return None


def _handle_llm(user_id: str, text: str):
    from app.services.llm import chat

    ref_date = _detect_ref_date(text)
    context = _build_context(user_id)
    if ref_date and ref_date != _today():
        day_rows = fetch_entries_for_date(user_id, ref_date)
        day_sum = fetch_day_summary(user_id, ref_date)
        context["ref_date"] = ref_date
        context["ref_date_foods"] = [
            f"{r['meal_slot']}: {r['food_name']} {r['kcal']:.0f}kcal"
            for r in day_rows
        ]
        context["ref_date_intake"] = day_sum["intake_kcal"]
        context["ref_date_burn"] = day_sum["burn_kcal"]

    try:
        result = chat(text, context, persona=load_user_persona(user_id))
    except Exception as e:
        logger.exception("LLM call failed: %s", e)
        return TextSendMessage(text=(
            "AI応答に失敗しました。少し待って再送するか、\n"
            "『朝 食パン100g 250kcal』形式で直接記録してください。\n"
            "『使い方』で全コマンドを確認できます"
        ))

    intent = result.get("intent", "chat")
    reaction = (result.get("reaction") or "").strip()

    if intent == "record" and result.get("foods"):
        m_date = DATE_PAT.match(text)
        rec_date = ref_date or _norm_date(m_date.groups() if m_date else None)
        tail = text[(m_date.end() if m_date else 0):].strip()

        slot_from_text = None
        for kw in ("朝食", "昼食", "夕食", "夜食", "間食"):
            if kw in tail:
                slot_from_text = _slot_to_english(kw)
                break
        if slot_from_text is None:
            for kw in ("朝", "昼", "夕", "夜", "間"):
                if kw in tail:
                    slot_from_text = _slot_to_english(kw)
                    break

        foods = result["foods"]
        slot = slot_from_text or result.get("meal_slot") or "snack"
        _pending[user_id] = {
            "foods": foods,
            "meal_slot": slot,
            "date": rec_date,
        }
        total_kcal = sum(float(f.get("kcal") or 0) for f in foods)
        lines = []
        if reaction:
            lines.append(reaction)
        lines.append("")
        lines.append(f"AI推定 (記録前の確認) → {rec_date} {slot}:")
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


def _n(v, unit=""):
    return f"{v:.0f}{unit}" if isinstance(v, (int, float)) else "-"


def _format_record(d, p):
    return (
        f"✅ 記録: {d} {p['meal_slot']} {p['food_name']}\n"
        f"   {p['kcal']:.0f}kcal / P{_n(p.get('protein_g'))} "
        f"F{_n(p.get('fat_g'))} C{_n(p.get('carb_g'))} "
        f"食塩{_n(p.get('salt_g'), 'g')}"
        f"\n   source=user_report / confidence=estimated"
    )

