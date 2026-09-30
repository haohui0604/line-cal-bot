"""日別ビュー用のAIコーチコメント生成 (Phase 3.5).

- ユーザー個人の人格設定 (user_settings) をそのまま声として使う
- トレーナーの⭐指導 (comments.is_directive) をプロンプトに常時注入
- report_comments テーブルをキャッシュとして流用
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
from app.services.gym_db import fetch_active_directives
from app.services.goals import context_line as goal_context_line
from app.services.day_view import SLOT_JP

logger = logging.getLogger(__name__)

MODELS = ("gemini-flash-latest", "gemini-2.0-flash", "gemini-flash-lite-latest")
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

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
    }
    facts["hash"] = hashlib.md5(
        json.dumps(facts, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:12]
    return facts


def _rule_based(f: dict) -> str:
    """LLM失敗時のフォールバック（事実のみ）."""
    bal = f["intake"] - f["burn"]
    sign = "超過" if bal > 0 else "以内"
    parts = [f"摂取 {f['intake']}kcal / 消費 {f['burn']}kcal（{abs(bal)}kcal{sign}）。"]
    if f.get("goal"):
        parts.append(f"目標は {f['goal']}kcal。")
    if not f["foods"]:
        parts.append("この日の食事記録はまだありません。")
    return "".join(parts)


def _generate(user_id: str, f: dict) -> str:
    if not _KEY:
        raise RuntimeError("GEMINI_API_KEY 未設定")
    persona = load_user_persona(user_id)
    directives = fetch_active_directives(user_id)
    goal_line = goal_context_line(user_id)

    dir_text = ""
    if directives:
        lines = "\n".join(f"- {d['body']}" for d in directives)
        dir_text = (
            "\n\n# 担当トレーナーからの指導方針（重要・必ず考慮し、"
            "自然に言及すること）\n" + lines)

    prompt = f"""あなたはユーザーの食事・運動に伴走するコーチ「{persona['bot_name']}」です。
一人称は「{persona['bot_pronoun']}」。性格・口調: {persona['bot_tone'] or '優しく励ます'}

# 対象日のデータ（確定値。改変・捏造は禁止）
- 日付: {f['date']}
- 摂取: {f['intake']}kcal（P{f['protein_g']}g F{f['fat_g']}g C{f['carb_g']}g）
- 消費: {f['burn']}kcal
- 目標摂取: {f['goal'] if f['goal'] else '未設定'}kcal
- 体重: {f['weight_kg'] if f['weight_kg'] else '不明'}kg
- 目的・目標: {goal_line or '未設定'}
- 食事内容: {', '.join(f['foods']) if f['foods'] else '記録なし'}
{dir_text}

# ルール
- 数値は与えられた値をそのまま引用する
- 医療的な診断・断定（痩せます、治ります等）は禁止
- ユーザーを責めない。「事実 → 次の一手（具体的行動）」の順で
- 120字以内のコメント本文のみを返す（前置き・JSON・記号装飾は不要）
"""
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
    """日次AIコメントを返す（キャッシュ優先）."""
    facts = _build_facts(user_id, target_date)
    key = f"dayview:{user_id}:{target_date}:{facts['hash']}"

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
