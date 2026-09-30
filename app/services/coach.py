"""日別ビュー用のAIコーチコメント生成 (Phase 3.5d).

コメントの順序（設計）:
  1. AIは自分の見立てを先に述べる（データ → 自分の判断）
  2. そのうえで、**過去の日付**に付いたトレーナーコメントが関係する場合だけ
     「〇〇さんも前に言ってたな」と一言添える
  3. 同じ日に付いたトレーナーコメントは、AIコメント生成時には参照しない
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
from app.services.goals import context_line as goal_context_line
from app.services.day_view import SLOT_JP
from app.services.dates import now_jst, today_jst

logger = logging.getLogger(__name__)

MODELS = ("gemini-flash-latest", "gemini-2.0-flash", "gemini-flash-lite-latest")
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

BUCKET_JP = {"morning": "朝", "noon": "昼", "evening": "夕方",
             "night": "夜", "final": "確定"}

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


def _situation_block(f: dict) -> str:
    b = f.get("time_bucket", "final")
    rem = f.get("remaining_kcal")
    rem_line = (f"- 目標までの残り摂取枠: {rem}kcal\n"
                if rem is not None else "")
    if b == "final":
        return ("# 状況\n"
                "- 対象日のデータは確定（1日の終わり、または過去日）。"
                "1日全体の総括としてコメントする。\n" + rem_line)
    return ("# 状況（1日の途中経過。確定値ではない）\n"
            f"- 現在の時刻帯: {BUCKET_JP[b]}\n"
            + rem_line
            + f"- 指示: {TIME_GUIDE[b]}\n"
              "- 途中経過なので『1日分が確定した』かのように低摂取を責めてはいけない。"
              "まだ記録されていない食事がある前提で話す。\n")


# 過去のトレーナーコメントを何日分さかのぼるか
PAST_COMMENT_DAYS = 14
PAST_COMMENT_LIMIT = 5

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
    # ハッシュに「過去のトレーナーコメント」を含める。
    # → 過去日にコメントが付くと翌日以降のキャッシュが自動で作り直される。
    #    同じ日にコメントが付いても、その日のキャッシュは変化しない（意図どおり）。
    facts["hash"] = hashlib.md5(
        json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:12]
    return facts


def _rule_based(f: dict) -> str:
    """LLM失敗時のフォールバック（事実のみ。時刻帯に応じた言い回し）."""
    b = f.get("time_bucket", "final")
    rem = f.get("remaining_kcal")
    if b == "final":
        bal = f["intake"] - f["burn"]
        sign = "超過" if bal > 0 else "以内"
        parts = [f"摂取 {f['intake']}kcal / 消費 {f['burn']}kcal"
                 f"（{abs(bal)}kcal{sign}）。"]
        if f.get("goal"):
            parts.append(f"目標は {f['goal']}kcal。")
        if not f["foods"]:
            parts.append("この日の食事記録はまだありません。")
        return "".join(parts)
    # 途中経過（朝/昼/夕方/夜）
    parts = [f"この時点（{BUCKET_JP[b]}）の摂取は {f['intake']}kcal。"]
    if rem is not None:
        parts.append(f"目標まであと {rem}kcal の枠がある。")
    if not f["foods"]:
        parts.append("まだ今日の記録がない。食べたら記録しておこう。")
    elif b == "noon":
        parts.append("夕食はこの残り枠を目安に選ぶとよい。")
    return "".join(parts)


def build_prompt(persona: dict, f: dict, goal_line: str = "",
                 directives=None) -> str:
    """AIコーチ用プロンプトを組み立てる（テスト可能なよう関数化）.

    重要: 「自分の見立て → トレーナーへの言及」の順序をプロンプトで明示する。
    """
    directives = directives or []
    past = f.get("past_trainer") or []

    past_block = ""
    if past:
        lines = []
        for p in past:
            who = p.get("author") or "トレーナー"
            mark = "（⭐方針）" if p.get("directive") else ""
            lines.append(f"- {p.get('date')} {who}: {p.get('body')}{mark}")
        past_block = (
            "\n# 過去の担当トレーナーからの指導（参考情報）\n"
            + "\n".join(lines) + "\n")
    elif directives:
        lines = "\n".join(f"- {d['body']}" for d in directives)
        past_block = (
            "\n# 過去の担当トレーナーからの指導（参考情報）\n"
            + lines + "\n")

    bucket = f.get("time_bucket", "final")
    situation = _situation_block(f)
    data_heading = ("# 対象日のデータ（確定値。改変・捏造は禁止）"
                    if bucket == "final"
                    else "# 対象日のこの時点のデータ（途中経過。改変・捏造は禁止）")
    return f"""あなたはユーザーの食事・運動に伴走するコーチ「{persona['bot_name']}」です。
一人称は「{persona['bot_pronoun']}」。性格・口調: {persona['bot_tone'] or '優しく励ます'}

# 話す順番（厳守）
1. まず、下の数値と「状況」だけを見て「あなた自身の見立て」を述べる。何が良くて何を変えるかは自分で判断する。
2. そのあとで、過去のトレーナー指導が今日の内容に本当に関係し、引用が助言の助けになる場合に限り、
   「{'{trainer}'}さんも前に言ってたな」のように一言だけ添える。
3. トレーナーの発言をそのまま繰り返す・要約して済ませるのは禁止。あなたの意見が主、トレーナーは補足。
4. トレーナー指導は毎回必ず引用する必要はない。関係が薄い・不要なら一切触れない。

{situation}
{data_heading}
- 日付: {f['date']}
- 摂取: {f['intake']}kcal（P{f['protein_g']}g F{f['fat_g']}g C{f['carb_g']}g）
- 消費: {f['burn']}kcal
- 目標摂取: {f['goal'] if f['goal'] else '未設定'}kcal
- 体重: {f['weight_kg'] if f['weight_kg'] else '不明'}kg
- 目的・目標: {goal_line or '未設定'}
- 食事内容: {', '.join(f['foods']) if f['foods'] else '記録なし'}
{past_block}
# ルール
- 数値は与えられた値をそのまま引用する
- 医療的な診断・断定（痩せます、治ります等）は禁止
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

    キャッシュキーは facts ハッシュ（当日データ + 過去のトレーナーコメント）
    に依存するため、
      - 当日のデータが変われば作り直される
      - 過去日にトレーナーコメントが付くと、翌日以降が作り直される
      - 同じ日にトレーナーコメントを付けても、その日のAIコメントは変わらない
    """
    facts = _build_facts(user_id, target_date)
    key = (f"dayview:{user_id}:{target_date}:"
           f"{facts['time_bucket']}:{facts['hash']}")

    try:
        cached = get_report_comment(key)
        if cached:
            comment = (cached.get("comment") if isinstance(cached, dict)
                       else cached[5])
            if comment:
                return {"comment": comment, "source": "cache"}
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
    return {"comment": text, "source": source}
