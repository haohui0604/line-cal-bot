"""日別ビュー用のAIコーチコメント生成 (Phase 3.5d / 3.5g).

コメントの設計:
  1. 「いま何が登録されているか」×「時刻帯」で状況（scenario）を1つだけ確定する。
     → 夕食を食べて登録した直後に「今日の夕食はあといくら」と言われる事故を防ぐ。
  2. AIは自分の見立てを先に述べる（データ → 自分の判断）
  3. そのうえで、**過去の日付**に付いたトレーナーコメントが関係する場合だけ
     「〇〇さんも前に言ってたな」と一言添える
  4. 同じ日に付いたトレーナーコメントは、AIコメント生成時には参照しない
     （トレーナーが後から付けた内容を、その日のAIコメントがなぞるのを防ぐ）

これにより「その日のデータが固まる → AIコメント → トレーナーコメント →
翌日以降のAIコメントがそれを考慮」という順序が保たれる。
"""
import hashlib
import json
import logging
import os

import httpx

from app.services.db import (
    fetch_day_summary, fetch_day_activity_total, fetch_entries_for_date,
    get_goal, fetch_latest_weight, load_user_persona,
    get_report_comment, save_report_comment,
)
from app.services.gym_db import fetch_active_directives, fetch_past_trainer_comments
from app.services.goals import context_line as goal_context_line, pfc_targets
from app.services.day_view import SLOT_JP
from app.services.dates import now_jst, today_jst

logger = logging.getLogger(__name__)

MODELS = ("gemini-flash-latest", "gemini-2.0-flash", "gemini-flash-lite-latest")
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

BUCKET_JP = {"morning": "朝", "noon": "昼", "evening": "夕方",
             "night": "夜", "final": "確定"}

# 食事の登録有無を判定する区分（間食・夜食は「その他」として扱う）
MEAL_SLOTS = ("breakfast", "lunch", "dinner")
SLOT_ORDER = ("breakfast", "lunch", "dinner", "snack", "night")

# 「間食・デザートに使ってよい」と提示する最小の残り枠。
# これ未満なら「追加で食べる提案」はしない（提案の空手形を防ぐ）。
DESSERT_MIN_KCAL = 150

# 時刻帯ごとの基本方針。食事の登録状況に応じた指示は _meal_state() 側で上書きする。
TIME_GUIDE = {
    "morning": ("今日はまだ朝。朝食の記録があれば労い、これからの1日の"
                "立て方（昼・夕の配分）を前向きに伝える。"),
    "noon":    ("昼時点の途中経過。ここまでの摂取と目標の残り枠を明示し、"
                "「あと◯◯kcalあるから夕食は◯◯系が食べられる」のように"
                "具体的な料理名で1〜2品提案する。"),
    "evening": ("夕方時点の途中経過。残り枠から夕食の選択肢を具体的に。"
                "間食が多い日は優しく指摘してよい。"),
    "night":   ("1日がほぼ終わった時点。今日の総括を労いつつ、"
                "明日への一手を添える。"),
    "final":   ("対象日の確定データとして1日を総括する。"),
}


def _time_bucket(target_date: str) -> str:
    """対象日が今日なら JST の時刻帯バケット、過去日なら 'final' を返す."""
    if target_date != today_jst():
        return "final"
    h = now_jst().hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 16:
        return "noon"
    if 16 <= h < 22:
        return "evening"
    return "night"


# ---- 状況（時刻帯 × 登録済みの食事）の確定 ----

def _logged_slots(f: dict) -> dict:
    """その日に実際に記録が入っている区分と kcal を返す（0kcal は記録なし扱い）."""
    return {k: v for k, v in (f.get("slot_kcal") or {}).items() if v}


def _meal_state(f: dict) -> dict:
    """「時刻帯 × 実際に登録されている食事区分」から状況を1つだけ決める.

    返り値の instruction が、その状況でAIがやるべき唯一の仕事。
    ここに無い場面（例: 夕食を食べ終えて登録済みなのに「夕食の残り枠」）は
    扱わせない。判定はすべてコード側で行い、LLMには選ばせない。
    """
    b = f.get("time_bucket") or _time_bucket(f.get("date", ""))
    slots = _logged_slots(f)
    has_b = bool(slots.get("breakfast"))
    has_l = bool(slots.get("lunch"))
    has_d = bool(slots.get("dinner"))
    has_any = bool(slots)
    rem = f.get("remaining_kcal")
    room = None
    if has_d and rem is not None and rem >= DESSERT_MIN_KCAL:
        room = int(rem)

    st = {"time_bucket": b, "has_breakfast": has_b, "has_lunch": has_l,
          "has_dinner": has_d, "has_any": has_any, "remaining_kcal": rem,
          "dessert_room_kcal": room, "forbid_dinner_budget": False}

    if b == "final":
        return {**st, "scenario": "final", "instruction": TIME_GUIDE["final"]}

    if b == "morning":
        if has_b:
            return {**st, "scenario": "morning_logged", "instruction": (
                "朝食は登録済み。朝食の内容を評価し、この後の昼・夕の配分の目安を"
                "前向きに伝える。")}
        if has_any:
            return {**st, "scenario": "morning_partial", "instruction": (
                "朝食以外の記録はあるが朝食がまだ。急かさず、"
                "食べたら登録するよう軽く促す。")}
        return {**st, "scenario": "morning_empty", "instruction": (
            "まだ何も登録されていない。一日の始まりなので、"
            "食べたら記録するよう無理なく促す。")}

    if b == "noon":
        if has_l:
            return {**st, "scenario": "lunch_logged", "instruction": (
                "昼食は登録済み。ここまでの摂取と目標の残り枠を示し、"
                "夕食に使える量と、それを踏まえた具体的な料理名を1〜2品提案する。")}
        if has_b or has_any:
            return {**st, "scenario": "lunch_missing", "instruction": (
                "昼食がまだ登録されていない。忙しかったのか、"
                "まだ登録していないだけなのかを責めずに尋ね、"
                "食べたなら記録するよう促す。")}
        return {**st, "scenario": "midday_empty", "instruction": (
            "朝食の記録も無いまま昼になった。『朝を抜いたのか、"
            "登録がまだなのか』を責めずに尋ね、食べた分を登録するよう促す。")}

    if b == "evening":
        if has_d:
            st = {**st, "forbid_dinner_budget": True,
                  "scenario": "dinner_logged"}
            if room:
                st["instruction"] = (
                    "夕食も登録済み。1日の収支を整理し、目標に残っている余裕"
                    "（間食・デザートに使ってよい量）を1つ示す。"
                    "夕食の『あと食べられる量』には触れない。")
            else:
                st["instruction"] = (
                    "夕食も登録済みで、今日はもう目標を超えている（または余裕が小さい）。"
                    "1日の収支を整理し、追加で食べる提案はせず明日の一手を1つ添える。")
            return st
        if has_l:
            return {**st, "scenario": "dinner_budget", "instruction": (
                "昼食まで登録済みで夕食がまだ。夕食にあと何kcal使えるかを示し、"
                "具体的な料理名を1〜2品提案する。")}
        if has_any:
            return {**st, "scenario": "dinner_missing", "instruction": (
                "夕食がまだ登録されていない（朝・昼の記録も少ない）。"
                "忙しかったのかを気遣いながら、食べたら記録するよう促す。")}
        return {**st, "scenario": "evening_empty", "instruction": (
            "夕方だがまだ何も登録されていない。"
            "『登録し忘れか、食べていないのか』を責めずに尋ねる。")}

    # night（夜〜深夜）
    if has_d:
        st = {**st, "forbid_dinner_budget": True,
              "scenario": "night_dinner_logged"}
        if room:
            st["instruction"] = (
                "夕食まで登録済み。1日の収支を整理して総括し、目標に余裕が"
                "残っているので間食・デザートに使ってよい量を1つ示す。"
                "夕食の『あと食べられる量』には触れない。")
        else:
            st["instruction"] = (
                "夕食まで登録済みで、今日はもう目標を超えている（または余裕が小さい）。"
                "1日の収支を整理し、追加で食べる提案はせず明日の一手を1つ添える。")
        return st
    if has_l or has_any:
        return {**st, "scenario": "night_no_dinner", "instruction": (
            "夜だが夕食がまだ登録されていない。"
            "『夕食を抜いたのか、登録がまだなのか』を責めずに尋ね、"
            "この時間の食事は軽めに、明日の一手を添える。")}
    return {**st, "scenario": "night_empty", "instruction": (
        "夜だがこの日の記録がほとんど無い。責めずに『登録し忘れか』を尋ね、"
        "明日は1食だけでも記録するよう促す。")}


def _situation_block(f: dict) -> str:
    """状況（scenario）と、その場面で使ってよい数値だけを渡すブロック."""
    st = f.get("meal_state") or _meal_state(f)
    b = st.get("time_bucket") or f.get("time_bucket") or "final"
    rem = f.get("remaining_kcal")
    bal = f.get("balance_kcal")

    lines = ["# 状況"]
    if b == "final":
        lines.append("- 対象日のデータは確定（1日の終わり、または過去日）。"
                     "1日全体の総括としてコメントする。")
    else:
        lines.append(f"- 現在の時刻帯: {BUCKET_JP[b]}"
                     "（今日の途中経過。確定値ではない）")

    slots = _logged_slots(f)
    label = " / ".join(
        f"{SLOT_JP.get(k, k)} {round(slots[k])}kcal"
        for k in SLOT_ORDER if slots.get(k)
    ) or "なし"
    lines.append(f"- 登録済みの食事区分: {label}")

    if rem is not None:
        lines.append(
            f"- 目標までの残り摂取枠: {rem}kcal（目標 {f.get('goal')}kcal"
            f" − 摂取 {f.get('intake')}kcal。プラスなら『あと食べてよい量』、"
            "マイナスなら『目標超過』）")
    if bal is not None:
        lines.append(
            f"- 摂取と消費の収支: {bal}kcal（摂取 {f.get('intake')}kcal"
            f" − 消費 {f.get('burn')}kcal。残り摂取枠とは別の指標）")
    if st.get("dessert_room_kcal") is not None:
        lines.append(f"- 目標に残っている余裕: {st['dessert_room_kcal']}kcal"
                     "（間食・デザートに使ってよい量の上限）")

    lines.append(f"- この状況でやること: {st['instruction']}")
    if st.get("forbid_dinner_budget"):
        lines.append("- 禁止: 夕食は登録済みなので、追加で食べられる量（残り枠）や"
                     "夕食の献立の提案はしない。")
    if b != "final":
        lines.append("- 途中経過なので『1日分が確定した』かのように低摂取を"
                     "責めてはいけない。まだ記録されていない食事がある前提で話す。")
    return "\n".join(lines) + "\n"


# 過去のトレーナーコメントを何日分さかのぼるか
PAST_COMMENT_DAYS = 14
PAST_COMMENT_LIMIT = 3

try:
    from app.config import settings
    _KEY = settings.GEMINI_API_KEY
except Exception:
    _KEY = os.environ.get("GEMINI_API_KEY", "")


def _build_facts(user_id: str, target_date: str) -> dict:
    s = fetch_day_summary(user_id, target_date)
    burn = fetch_day_activity_total(user_id, target_date)
    goal = get_goal(user_id, target_date)
    entries = fetch_entries_for_date(user_id, target_date)
    weight_kg = None
    try:
        w = fetch_latest_weight(user_id, on_or_before=target_date)
        if w:
            weight_kg = w["weight_kg"] if isinstance(w, dict) else w[1]
    except Exception:
        pass
    foods = [
        f"{SLOT_JP.get(e.get('meal_slot'), '間食')}:{e.get('food_name')}"
        f" {round(e.get('kcal') or 0)}kcal"
        for e in entries
    ]
    slot_kcal: dict = {}
    for e in entries:
        sl = e.get("meal_slot") or "snack"
        slot_kcal[sl] = round(slot_kcal.get(sl, 0) + (e.get("kcal") or 0))
    # 対象日より「前」の日付に付いたトレーナーコメントのみ（当日分は含めない）
    try:
        past = fetch_past_trainer_comments(
            user_id, before_date=target_date,
            days=PAST_COMMENT_DAYS, limit=PAST_COMMENT_LIMIT)
    except Exception:
        logger.exception("fetch_past_trainer_comments failed")
        past = []
    facts = {
        "date": target_date,
        "intake": round(s.get("intake_kcal") or 0),
        "burn": round(burn or 0),
        "goal": round(goal) if goal else None,
        "protein_g": round(s.get("protein_g") or 0, 1),
        "fat_g": round(s.get("fat_g") or 0, 1),
        "carb_g": round(s.get("carb_g") or 0, 1),
        "weight_kg": weight_kg,
        "foods": foods,
        "slot_kcal": slot_kcal,
        "past_trainer": [
            {"date": p.get("target_date"), "author": p.get("author_name"),
             "body": p.get("body"), "directive": bool(p.get("is_directive"))}
            for p in past
        ],
    }
    # 時刻帯（今日のみ朝/昼/夕方/夜）と目標残り枠。ハッシュに含めるため、
    # 時刻帯が切り替わるとその日のコメントも自動で作り直される。
    facts["time_bucket"] = _time_bucket(target_date)
    facts["remaining_kcal"] = (
        facts["goal"] - facts["intake"]) if facts.get("goal") else None
    # 摂取 − 消費（マイナス＝消費が上回っている）。
    # 「目標までの残り枠」とは別の指標なので、プロンプトでも分けて渡す。
    facts["balance_kcal"] = facts["intake"] - facts["burn"]
    # 目標PFC（目安）。設定が無ければ体重・目標kcalから算出する。
    try:
        t = pfc_targets(user_id, target_kcal=facts.get("goal"))
    except Exception:
        logger.exception("pfc_targets failed")
        t = None
    if t:
        facts["pfc_target"] = t
        pct = {}
        for short, key, got in (("protein", "protein_g", facts["protein_g"]),
                                ("fat", "fat_g", facts["fat_g"]),
                                ("carb", "carb_g", facts["carb_g"])):
            tgt = t.get(key)
            if tgt:
                pct[short] = f"{round(got / tgt * 100)}%"
        if pct:
            facts["pfc_pct"] = pct
    # 時刻帯 × 登録済みの食事区分 → 状況を1つに確定（LLMには選ばせない）
    facts["meal_state"] = _meal_state(facts)
    # ハッシュに「過去のトレーナーコメント」を含める。
    # → 過去日にコメントが付くと翌日以降のキャッシュが自動で作り直される。
    #    同じ日にコメントが付いても、その日のキャッシュは変化しない（意図どおり）。
    facts["hash"] = hashlib.md5(
        json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:12]
    return facts


def _rule_based(f: dict) -> str:
    """LLM失敗時のフォールバック（事実のみ。状況ごとの言い回し）."""
    st = f.get("meal_state") or _meal_state(f)
    scenario = st.get("scenario", "final")
    b = st.get("time_bucket") or f.get("time_bucket", "final")
    rem = f.get("remaining_kcal")

    if scenario == "final":
        bal = f["intake"] - f["burn"]
        sign = "超過" if bal > 0 else "以内"
        parts = [f"摂取 {f['intake']}kcal / 消費 {f['burn']}kcal"
                 f"（{abs(bal)}kcal{sign}）。"]
        if f.get("goal"):
            diff = f["goal"] - f["intake"]
            parts.append(f"目標は {f['goal']}kcal、"
                         + (f"あと {diff}kcal の余裕があります。" if diff >= 0
                            else f"{abs(diff)}kcal 超過しています。"))
            if diff > f["goal"] * 0.3:
                parts.append("摂取がやや少なめなので、無理に削らず"
                             "必要な分は補いましょう。")
        if not f["foods"]:
            parts.append("この日の食事記録はまだありません。")
        return "".join(parts)

    # 夕食まで登録済み: 収支＋（余裕があるときだけ）デザート枠。
    # 夕食の残り枠には触れない（食べ終えた後の「あと夕食に◯◯」を禁止）。
    if scenario in ("dinner_logged", "night_dinner_logged"):
        bal = f["intake"] - f["burn"]
        sign = "超過" if bal > 0 else "以内"
        parts = [f"摂取 {f['intake']}kcal / 消費 {f['burn']}kcal"
                 f"（{abs(bal)}kcal{sign}）。"]
        room = st.get("dessert_room_kcal")
        if room:
            parts.append(f"目標にはまだ {room}kcal の余裕があるので、"
                         "間食やデザートに使ってよい量です。")
        elif rem is not None:
            if rem >= 0:
                parts.append(f"目標まであと {rem}kcal。"
                             "追加で食べるならこの範囲で。")
            else:
                parts.append(f"今日は目標を {abs(rem)}kcal 超えています。"
                             "追加は控えめにしましょう。")
        parts.append("明日もこの調子で記録を続けましょう。")
        return "".join(parts)

    # 状況だけを尋ねる場面でも、当日の数値は必ず添える（判断材料を消さない）
    data = (f" 今日の摂取は {f['intake']}kcal、消費は {f['burn']}kcal"
            + (f"（目標 {f['goal']}kcal）。" if f.get("goal") else "。"))

    # 昼食がまだ → 「抜いたのか、登録がまだか」を尋ねる
    if scenario == "midday_empty":
        return ("昼の時点で朝食・昼食の記録がまだありません。"
                "朝を抜いたのか、登録がまだなのか教えてください。"
                "食べた分を記録すると、夕食の目安を出せます。" + data)
    if scenario == "lunch_missing":
        return ("昼食の記録がまだありません。忙しかったのか、"
                "登録がまだなのか教えてください。"
                "食べたなら記録しておくと、夕食の目安を出せます。" + data)

    # 夕食がまだ・夜に記録なし
    if scenario in ("dinner_missing", "evening_empty"):
        return ("夕食の記録がまだありません。忙しかったのか、"
                "登録がまだなのか教えてください。"
                "食べたら記録しておくと、今日の収支をまとめられます。" + data)
    if scenario == "night_no_dinner":
        return ("夜になりましたが、夕食の記録がまだありません。"
                "夕食を抜いたのか、登録がまだなのか教えてください。"
                "この時間の食事は軽めにして、明日の記録につなげましょう。" + data)
    if scenario == "night_empty":
        return ("今日はまだ記録がありません。登録し忘れではありませんか。"
                "明日は1食だけでも記録すると、流れが見えてきます。" + data)
    if scenario in ("morning_empty", "morning_partial"):
        return ("この時点ではまだ記録がありません。"
                "食べたら記録しておきましょう。" + data)

    # 途中経過（朝食・昼食が登録済みで夕食がまだ）
    parts = [f"この時点（{BUCKET_JP[b]}）の摂取は {f['intake']}kcal。"]
    if rem is not None and rem > 0:
        parts.append(f"目標まであと {rem}kcal の枠がある。")
        if scenario in ("lunch_logged", "dinner_budget"):
            parts.append(f"夕食にはあと {rem}kcal 使える。")
    if not _logged_slots(f):
        parts.append("まだ今日の記録がない。食べたら記録しておこう。")
    elif b == "noon":
        parts.append("夕食はこの残り枠を目安に選ぶとよい。")
    return "".join(parts)


def build_prompt(persona: dict, f: dict, goal_line: str = "",
                 directives=None) -> str:
    """AIコーチ用プロンプトを組み立てる（テスト可能なよう関数化）.

    重要: 「自分の見立て → トレーナーへの言及」の順序をプロンプトで明示する。
    状況（時刻帯 × 登録済みの食事）はコード側で1つに確定し、
    ここでは「その場面だけ」を扱わせる。
    """
    directives = directives or []
    past = f.get("past_trainer") or []

    def _clean(body) -> str:
        """引用文を1行・短めに整える（改行や鉤括弧、乱暴な長文をそのまま載せない）."""
        t = " ".join(str(body or "").split()).strip("「」『』\"'　")
        return t[:60] + ("…" if len(t) > 60 else "")

    past_block = ""
    if past:
        lines = []
        for p in past:
            who = p.get("author") or "トレーナー"
            mark = "（⭐方針）" if p.get("directive") else ""
            lines.append(f"- {p.get('date')} {who}: {_clean(p.get('body'))}{mark}")
        past_block = (
            "\n# 過去の担当トレーナーからの指導（参考情報・発言そのもの）\n"
            + "\n".join(lines) + "\n"
            "※ ここに並ぶのはトレーナーの生の言葉。そのまま復唱せず、"
            "敬意を保った言い方に直して必要な分だけ触れる。"
            "乱暴・断定的な言い回しや、今日の内容と無関係な発言は引用しない。\n")
    elif directives:
        lines = "\n".join(f"- {_clean(d['body'])}" for d in directives)
        past_block = (
            "\n# 過去の担当トレーナーからの指導（参考情報・発言そのもの）\n"
            + lines + "\n"
            "※ 上と同様、そのまま復唱せず敬意を保った言い方に直すこと。\n")

    st = f.get("meal_state") or _meal_state(f)
    bucket = st.get("time_bucket") or f.get("time_bucket", "final")
    situation = _situation_block(f)
    data_heading = ("# 対象日のデータ（確定値。改変・捏造は禁止）"
                    if bucket == "final"
                    else "# 対象日のこの時点のデータ（途中経過。改変・捏造は禁止）")

    pfc_block = ""
    t = f.get("pfc_target")
    if t:
        pfc_block += ("- PFC目標（目安）: " + " / ".join(
            f"{name} {v}g" for name, v in
            (("P", t.get("protein_g")), ("F", t.get("fat_g")),
             ("C", t.get("carb_g"))) if v) + "\n")
        pct = f.get("pfc_pct") or {}
        if pct:
            pfc_block += ("- PFC達成率: " + " / ".join(
                f"{name} {v}" for name, v in
                (("P", pct.get("protein")), ("F", pct.get("fat")),
                 ("C", pct.get("carb"))) if v) + "\n")

    return f"""あなたはユーザーの食事・運動に伴走するコーチ「{persona['bot_name']}」です。
一人称は「{persona['bot_pronoun']}」で文全体を通して固定する。
性格・口調: {persona['bot_tone'] or '優しく励ます'}。
口調は指定に合わせつつ、文末表現（「〜じゃ」「〜だ」など）を1つの文体に統一し、
文中で混ぜない。古風・難解な語（例:「大儀」「〜であった」）や誇張した言い回しは使わず、
日常的で読みやすい言葉で書く。

# 話す順番（厳守）
1. まず、下の数値と「状況」だけを見て「あなた自身の見立て」を述べる。何が良くて何を変えるかは自分で判断する。
2. そのあとで、下のトレーナー指導の一覧が今日の内容に本当に関係し、引用が助言の助けになる場合に限り、
   一覧にある実際の名前を使って「◯◯さんも前に言ってたな」のように一言だけ添える（◯◯は実名。空欄や記号は書かない）。
3. そのときも、乱暴・断定的な言い回しや今日の内容と無関係な発言は引用しない。敬意を保った言い方に直し、長く復唱しない。
4. トレーナーの発言をそのまま繰り返す・要約して済ませるのは禁止。あなたの意見が主、トレーナーは補足。
5. トレーナー指導は毎回必ず引用する必要はない。関係が薄い・不要なら一切触れない。

{situation}
{data_heading}
- 日付: {f['date']}
- 摂取: {f['intake']}kcal（P{f['protein_g']}g F{f['fat_g']}g C{f['carb_g']}g）
- 消費: {f['burn']}kcal
- 目標摂取: {f['goal'] if f['goal'] else '未設定'}kcal
- 体重: {f['weight_kg'] if f['weight_kg'] else '不明'}kg
- 目的・目標: {goal_line or '未設定'}
- 食事内容: {', '.join(f['foods']) if f['foods'] else '記録なし'}
{pfc_block}{past_block}
# 数値の意味（この前提で解釈する）
- 「目標摂取」はその日の摂取目標。達成度は 摂取 ÷ 目標 で見る。
- 「残り摂取枠 ＝ 目標 − 摂取」。プラスは「あと食べてよい量」、マイナスは「目標超過」。
- 「収支 ＝ 摂取 − 消費」。マイナスは「消費が摂取を上回っている」状態で、
  残り摂取枠とは別の指標。この2つを同じ文で混同しない。
- 「不足」と言えるのは、摂取が目標の70%未満、または摂取が1000kcal未満のとき
  （レポート側と同じ基準）。消費が摂取を上回っていても、摂取が目標を
  満たしていれば「不足」とは書かない。
- 摂取不足そのものを「見事」「立派」と褒めない。称賛は目標達成や記録の継続に対して行う。
- PFC（たんぱく質・脂質・炭水化物）の量を「多い／少ない」と評価できるのは、
  目標値や基準が上のデータに示されている場合だけ。示されていなければ
  数値を挙げるにとどめ、良し悪しを断定しない。

# ルール
- 上の「状況」で示された場面だけを扱う。示されていない場面
  （例: 夕食を食べ終えて登録済みなのに「夕食にあと◯◯kcal」）は書かない。
- 数値は与えられた値をそのまま引用する（言い換え・概算・作り直しは禁止）
- 事実の復唱より示唆を優先する。摂取kcal・PFCの数値はグラフと実績欄に出ているので、
  「◯◯kcalでした」と言い直すだけにしない。その数字が意味することを1文で示し、
  次の一手（具体的な行動・品目の置き換え）を1つ添える。
- 良い例1: 「目標は超えましたが、運動ぶんが効いて収支はマイナス。この調子で続けましょう」
- 良い例2: 「脂質と塩分が多めでした。次は揚げ物を焼き物に替えると抑えられます」
- 医療・診断・治療効果の断定は禁止（薬機法・健康増進法に配慮）。「痩せる」「治る」
  「◯◯に効く」は使わず、「〜しやすくなります」「〜を意識すると抑えやすいです」のように
  生活習慣の提案として書く。
- 医療的な診断・断定（痩せます、治ります等）は禁止
- 「見事だ」と「不足しすぎ」のように、矛盾する評価を1つのコメントに並べない
- ユーザーを責めない。「事実 → 次の一手（具体的行動）」の順で
- 120字以内のコメント本文のみを返す（前置き・JSON・記号装飾は不要）
"""


def _generate(user_id: str, f: dict) -> str:
    if not _KEY:
        raise RuntimeError("GEMINI_API_KEY 未設定")
    persona = load_user_persona(user_id)
    directives = fetch_active_directives(user_id)
    goal_line = goal_context_line(user_id)
    prompt = build_prompt(persona, f, goal_line=goal_line,
                          directives=directives)
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 512},
    }
    last_err = None
    for model in MODELS:
        try:
            r = httpx.post(
                f"{API_BASE}/{model}:generateContent?key={_KEY}",
                json=payload, timeout=15.0)
            r.raise_for_status()
            text = (r.json()["candidates"][0]["content"]["parts"][0]["text"]
                    .strip())
            if text:
                return text
        except Exception as e:
            last_err = e
            logger.warning("coach generate failed on %s: %s", model, e)
    raise RuntimeError(f"all models failed: {last_err}")


def get_day_comment(user_id: str, target_date: str) -> dict:
    """日次AIコメントを返す（キャッシュ優先）.

    キャッシュキーは facts ハッシュ（当日データ + 登録済みの食事区分 +
    時刻帯 + 過去のトレーナーコメント）に依存するため、
      - 食事を登録すれば作り直される（＝夕食を登録した直後に
        「夕食はあと◯◯」と言われる事故が起きない）
      - 時刻帯が変われば作り直される
      - 過去日にトレーナーコメントが付くと、翌日以降が作り直される
      - 同じ日にトレーナーコメントを付けても、その日のAIコメントは変わらない
    """
    facts = _build_facts(user_id, target_date)
    st = facts.get("meal_state") or {}
    key = (f"dayview:{user_id}:{target_date}:"
           f"{facts['time_bucket']}:{st.get('scenario', '')}:{facts['hash']}")

    try:
        cached = get_report_comment(key)
        if cached:
            comment = (cached.get("comment") if isinstance(cached, dict)
                       else cached[5])
            if comment:
                return {"comment": comment, "source": "cache",
                        "scenario": st.get("scenario")}
    except Exception:
        pass

    try:
        text = _generate(user_id, facts)
        source = "ai"
    except Exception:
        logger.exception("coach generate failed; fallback to rule")
        text = _rule_based(facts)
        source = "rule"

    try:
        save_report_comment(
            cache_key=key, user_id=user_id, scope="dayview",
            period_key=target_date, headline="", comment=text, advice="",
            source=source,
            facts_json=json.dumps(facts, ensure_ascii=False))
    except Exception:
        logger.exception("cache save failed")
    return {"comment": text, "source": source, "scenario": st.get("scenario")}
