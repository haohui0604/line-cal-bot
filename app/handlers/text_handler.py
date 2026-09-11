"""テキスト入力 → commands / records / summary / LLM chat 振り分け."""
import logging
import re
from datetime import date

from linebot.models import TextSendMessage, FlexSendMessage

from app.services.db import (
    save_entry, save_activity, save_weight, set_goal, get_goal,
    fetch_day_summary, fetch_recent_history, fetch_today_food_names,
    update_entry_kcal, find_entry_candidates,
    fetch_latest_weight, fetch_weight_series,
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

# ---- パターン集 ----
GOAL_PAT = re.compile(r"^目標(?:kcal)?\s*[:：]?\s*(\d+)")
# 体重: 日付指定可 + 「推定」フラグ
# 例:「体重 72.5」「8/29 体重 75.8」「8/26 体重 76.5推定」
WEIGHT_PAT = re.compile(
    r"^(?:(\d{1,2})/(\d{1,2})\s+)?体重\s+([0-9]+(?:\.[0-9]+)?)(推定)?"
    r"(?:\s+(?:体脂肪|F|脂肪)\s*([0-9]+(?:\.[0-9]+)?))?"
    r"(?:\s+(?:筋肉|M)\s*([0-9]+(?:\.[0-9]+)?))?"
    r"(?:\s+(?:BMR|基礎代謝)\s*([0-9]+))?"
)
# 消費: 日付指定可。例:「運動 320」「9/10 消費 2188 活動195 安静1993」
ACTIVITY_PAT = re.compile(
    r"^(?:(\d{1,2})/(\d{1,2})\s+)?(?:運動|活動|消費)\s+([0-9]+)(?:\s*kcal)?"
    r"(?:\s+(?:活動|active|ACTIVE)\s*([0-9]+))?"
    r"(?:\s+(?:安静|resting|RESTING|基礎代謝|basal)\s*([0-9]+))?"
)
# 修正: 「修正 9/9 昼 牛丼並盛 720」 / 「修正 9/9 牛丼 720」
MODIFY_PAT = re.compile(
    r"^修正\s+(\d{1,2})/(\d{1,2})"
    r"(?:\s+(朝食|昼食|夕食|夜食|間食|朝|昼|夜|夕|間))?"
    r"\s+(\S+?)\s+(\d+(?:\.\d+)?)(?:\s*kcal)?\s*$",
    re.IGNORECASE,
)
# 修正コマンドの複数候補からの番号選択
MODIFY_CONFIRM_PAT = re.compile(r"^修正候補\s+(\d+)\s*$")

# 「牛丼 9/9 昼 1020kcal」のような末尾日付+スロット指定の記録用
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
    "\n"
    "【記録】\n"
    "・『朝 食パン100g 250kcal』→ 即記録\n"
    "・『ラーメン食べた』→ AI推定(確認あり)\n"
    "・写真送信 → 料理/成分表/体重計/消費kcalを読取\n"
    "・『間食 プロテインバー 180kcal』→ 間食区分\n"
    "\n"
    "【過去データ】\n"
    "・『一括』→ 複数行貼付け →『確定』\n"
    "・『修正 9/9 昼 牛丼 720kcal』→ 過去記録の修正\n"
    "・『9/10 消費 2188』『8/29 体重 75.8』→ 日付指定可\n"
    "\n"
    "【体組成・運動】\n"
    "・『体重 72.5 体脂肪18 筋肉52 BMR1500』\n"
    "・『体重』→ 直近7日の推移（未入力日は直前値で表示）\n"
    "\n"
    "【確認】\n"
    "・『集計』『履歴』『週次』『月次』\n"
    "・『目標 2000』→ 目標設定 /『目標』→ 確認\n"
    "\n"
    "【相談】\n"
    "・『あと何kcal食べていい？』など自由文でAI相談"
)

# 初期設定ウィザードの回答語彙
SETUP_PURPOSES = {
    "1": "減量", "減量": "減量", "痩せたい": "減量", "やせたい": "減量",
    "2": "維持", "維持": "維持", "現状維持": "維持",
    "3": "増量", "増量": "増量", "筋肉": "増量",
}

# LLM 推定の未確定レコード (Render 再起動で消える簡易実装)
_pending: dict = {}           # user_id -> {"foods": [...], "meal_slot": str, "date": str}
_modify_pending: dict = {}    # user_id -> {"candidates": [...], "new_kcal": float}
_bulk_pending: dict = {}      # user_id -> {"rows": [parsed, ...]}
_setup_pending: dict = {}     # user_id -> {"step": int, "data": dict}


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
                f"⚠ 「維持」を選択しましたが目標体重{goal_w}kgは"
                f"現在より{weight - goal_w:.1f}kg低い設定です。"
                "減量目的に切り替えますか？ →『初期設定』をやり直し")
    elif purpose == "増量":
        target = maintenance + 300
    target = max(1200.0, min(target, 4000.0))
    set_goal(user_id=user_id, date=_today(), target_kcal=target)

    lines = ["🎯 初期設定が完了しました！", "",
             f"目的: {purpose}",
             f"現体重: {weight}kg"]
    if goal_w is not None:
        lines.append(f"目標体重: {goal_w}kg")
    if months:
        lines.append(f"期間: {months}ヶ月")
    lines += ["",
              f"維持カロリー(推定): {maintenance:.0f}kcal/日",
              f"→ 目標摂取カロリー: {target:.0f}kcal/日"]
    lines += warn
    lines += ["",
              "※維持カロリーは簡易推定（体重×33）です。"
              "毎日『消費 ○○○○』を記録すると赤字計算の精度が上がります",
              "変更はいつでも『目標 1800』『初期設定』でできます"]
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

        # 0) LLM 推定の確定/取消 (pending は日付も保持)
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

        # 0.5) 修正コマンドの複数候補からの番号選択
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
                    "Q2. 現在の体重は？（例: 75.8）"
                ))

            if st["step"] == 2:
                m2 = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:kg)?$", text)
                if not m2:
                    return TextSendMessage(text="体重を数値で入力してください（例: 75.8）")
                w = float(m2.group(1))
                if w < 30 or w > 250:
                    return TextSendMessage(text="30〜250の範囲で入力してください")
                st["data"]["weight"] = w
                save_weight(user_id=user_id, date=_today(), weight_kg=w,
                            is_measured=1, note="initial_setup")
                st["step"] = 3
                return TextSendMessage(text=(
                    f"現体重 {w}kg を記録しました。\n\n"
                    "Q3. 目標体重は？（例: 72）\n"
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
            # モード中の再入ガード: 読込済みデータを消さない
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
                "9/1 朝 食パン 250kcal\n"
                "9/1 昼 牛丼並盛 700kcal\n"
                "9/1 夜 鶏むね150g\n\n"
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

                # kcal 無し行の推定: まず batch → 失敗時に per-item
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

            # モード中の入力 = 過去ログ行としてパース
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

        # 3.5) 体重推移（未入力日は直前値で繰越し表示）
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
            return FlexSendMessage(
                alt_text=f"直近30日 レポート (目標 {tgt:.0f}kcal)",
                contents=monthly_summary_flex(rows, target_kcal=tgt),
            )

        # 5) 目標摂取カロリー
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
                "変更: 『目標 1800』のように送ってください\n"
                "自動計算: 『初期設定』から目的と体重を入力"
            ))

        # 6) 体重記録 (日付指定可・「推定」フラグ対応)
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
            return TextSendMessage(text=base + "\n『体重』で推移を確認できます")

        # 7) 活動(消費)カロリー（日付指定可）
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
            return TextSendMessage(text=(
                f"🏃 活動記録: {rec_date} {total:.0f}kcal{extra}\n"
                f"消費 {s['burn_kcal']:.0f}kcal / 摂取 {s['intake_kcal']:.0f}kcal\n"
                f"赤字 {s['deficit_kcal']:+.0f}kcal"
            ))

        # 8) 過去データ修正（旧kcal表示 / 3段フォールバック検索）
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

        # 9) 末尾日付付きの食事記録 (確認フロー・日付保持)
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

        # 10) ルールベース食事登録 (kcal 明記時のみ即保存)
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
    except Exception:
        logger.exception("LLM call failed")
        return TextSendMessage(text=(
            "AI応答に失敗しました。少し待って再送するか、\n"
            "『朝 食パン100g 250kcal』形式で直接記録してください。\n"
            "『使い方』で全コマンドを確認できます"
        ))

    intent = result.get("intent", "chat")
    reaction = (result.get("reaction") or "").strip()

    if intent == "record" and result.get("foods"):
        # -----------------------------------------------------------
        # 【修正】入力テキストの前置きから日付とスロットを抽出。
        # _pending にはこの値で保存し、_save_foods() で正しい日付に入る。
        # -----------------------------------------------------------
        m_date = DATE_PAT.match(text)
        rec_date = _norm_date(m_date.groups() if m_date else None)
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
            "date": rec_date,        # ← 抽出日付 (前置きの 9/10 が入る)
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
