"""レポート（今日/今週/今月）の「AIひとこと」を生成する。

1. 数値は build_*_facts() がコード側で確定。LLM には言語化だけさせる。
2. 出力は JSON 固定（headline / comment / advice）。
3. 同一ユーザー・同一期間・同一集計値ならキャッシュを返す。
4. API 失敗 / JSON 崩れ / 禁止語 / 数値捏造 → report_comment.py へ自動フォールバック。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

FAT_KCAL_PER_KG = 7200
FAT_NOTE = "体脂肪1kg≒約7,000〜7,200kcal換算の目安"
MODELS = ("gemini-flash-latest", "gemini-2.0-flash", "gemini-flash-lite-latest")
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT_SEC = 8.0
MAX_OUTPUT_TOKENS = 1024
TEMPERATURE = 0.6

FORBIDDEN = ("痩せます", "痩せる", "治ります", "治る", "病気です", "確実に", "必ず痩せ")
LIMITS = {"headline": 24, "comment": 90, "advice": 60}

try:
    from app.config import GEMINI_API_KEY  # type: ignore
except Exception:
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

COMMON_RULES = """あなたはユーザーの食事・運動・体重に伴走する管理栄養士兼パーソナルコーチです。
与えられた「確定値」だけを根拠に、日本語で短いコメントを書きます。

# 絶対ルール
1. 数値を計算・改変・丸め直ししない。与えられた数値をそのまま引用する。
2. 与えられていない事実（食べた物・体調・天気・予定・疾患など）を創作しない。
3. 医療的な診断や断定（「痩せます」「治ります」等）を書かない。
   摂取不足や急激な減量には「専門家に相談」を促すにとどめる。
4. 体脂肪の換算に触れるときは必ず「目安」と明記する。
5. ユーザーを責めない。「事実 → 次の一手（具体的な行動）」の順で書く。
6. 出力は次の JSON のみ。前後に説明文・コードフェンス・改行を付けない。
{"headline": "…", "comment": "…", "advice": "…"}

# 文字数
- headline: 全角24字以内。絵文字は0〜1個。
- comment : 全角90字以内。
- advice  : 全角60字以内。明日すぐ実行できる具体行動を1つ。"""

DAILY_USER = """# 集計スコープ
日次レポート（1日分）

# 確定値（この数値だけを使う）
{facts}

# 書き方
- 摂取達成率が70%未満、または摂取が1000kcal未満なら「摂取不足」を最優先で指摘する。
- 運動消費が500kcal以上なら運動を称える。200〜499kcalなら軽く触れる。
- 「赤字kcal」が1以上の場合のみ体脂肪換算に触れる。0なら体脂肪に触れなくてよい。
- 確定値に無い数値（体重など）は書かない。
- advice は「明日の朝は◯◯にする」のように実行可能な1手にする。"""

WEEKLY_USER = """# 集計スコープ
週次レポート（7日分）

# 確定値（この数値だけを使う）
{facts}

# 書き方
- 単日のブレではなく「7日間の傾向」を語る。
- 記録日数が7日なら継続を称える。5日未満なら記録継続の一手を advice に置く。
- 週平均の摂取達成率が115%超なら食べすぎ傾向、75%未満なら摂取不足に触れる。
- 体重変化が −1.0kg を超えて減っている場合は「急すぎる」と注意し、摂取を戻す助言を書く。
- 体重変化が −0.3〜−1.0kg なら「ちょうどいいペース」と称える。
- 確定値に無い数値は書かない。"""

MONTHLY_USER = """# 集計スコープ
月次レポート（当月累積）

# 確定値（この数値だけを使う）
{facts}

# 書き方
- 月間の累積成果（赤字合計→体脂肪換算）と、生活の型（記録率）の2点を必ず触れる。
- 記録率80%以上は称賛、50%未満は「夜だけ送る」などハードルを下げる提案を advice に置く。
- 体重変化が −4% を超える減量、または月平均摂取が目標の75%未満なら、
  減量ペースが速すぎる旨と専門家への相談を必ず書く。
- 体重変化が −1〜−4% なら「健康的なペース」、±1%以内なら「維持も成果」と書く。
- 確定値に無い数値は書かない。"""

TEMPLATES = {"daily": DAILY_USER, "weekly": WEEKLY_USER, "monthly": MONTHLY_USER}

PERSONA_BLOCK = """
# 人格（口調だけを合わせる。数値・事実は変えない）
- 名乗り  : {bot_name}
- 一人称  : {pronoun}
- 性格・語尾: {tone}"""

DEFAULT_PERSONA = {"bot_name": "あなたのBot", "pronoun": "わたし", "tone": "やさしく親しみやすい丁寧語"}


def _f(v: Optional[float]) -> str:
    if v is None:
        return "未記録"
    v = float(v)
    return str(int(v)) if v == int(v) else f"{v:.1f}"


def build_daily_facts(*, date_label: str, intake: float, target: float, burn: float = 0.0,
                      protein=None, fat=None, carb=None, meals: Optional[list] = None) -> dict:
    intake_pct = round(intake / target * 100) if target else 0
    food_remaining = int(target - intake)
    net_remaining = int(target - intake + burn)
    deficit = max(0, net_remaining)
    fat_kg = round(deficit / FAT_KCAL_PER_KG, 2)
    facts = {
        "対象日": date_label,
        "目標摂取kcal": int(target),
        "摂取kcal": int(intake),
        "摂取達成率": f"{intake_pct}%",
        "運動消費kcal": int(burn),
        "食事だけの残りkcal": food_remaining,
        "運動込みの残りkcal": net_remaining,
        "赤字kcal": deficit,
        "体脂肪換算": f"約{fat_kg}kg分",
    }
    if protein is not None or fat is not None or carb is not None:
        facts["PFC(g)"] = f"P {_f(protein)}g / F {_f(fat)}g / C {_f(carb)}g"
    if meals:
        facts["記録した食事"] = " / ".join(meals)
    return facts


def build_weekly_facts(*, period_label: str, total_intake: float, logged_days: int, target: float,
                       total_burn: float = 0.0, entries_deficit: float = 0.0,
                       weight_start=None, weight_end=None) -> dict:
    avg = total_intake / logged_days if logged_days else 0.0
    avg_pct = round(avg / target * 100) if target else 0
    wd = None if (weight_start is None or weight_end is None) else round(weight_end - weight_start, 1)
    facts = {
        "対象期間": period_label,
        "目標摂取kcal": int(target),
        "記録日数": f"{logged_days}日 / 7日",
        "週間摂取合計kcal": int(total_intake),
        "記録日あたりの平均摂取kcal": f"{_f(avg)}kcal",
        "週平均の摂取達成率": f"{avg_pct}%",
        "週間の運動消費kcal": int(total_burn),
        "週間の赤字合計kcal": int(entries_deficit),
        "体脂肪換算": f"約{round(entries_deficit / FAT_KCAL_PER_KG, 2)}kg分",
    }
    if wd is not None:
        facts["週初の体重kg"] = _f(weight_start)
        facts["週末の体重kg"] = _f(weight_end)
        facts["体重変化"] = f"{'+' if wd > 0 else ''}{wd}kg"
    else:
        facts["体重変化"] = "未記録"
    return facts


def build_monthly_facts(*, period_label: str, total_intake: float, logged_days: int, elapsed_days: int,
                        target: float, total_burn: float = 0.0, entries_deficit: float = 0.0,
                        weight_start=None, weight_end=None) -> dict:
    avg = total_intake / logged_days if logged_days else 0.0
    avg_pct = round(avg / target * 100) if target else 0
    rate = round(logged_days / elapsed_days * 100) if elapsed_days else 0
    wd = None if (weight_start is None or weight_end is None) else round(weight_end - weight_start, 1)
    pct = round(wd / weight_start * 100, 1) if (wd is not None and weight_start) else None
    facts = {
        "対象期間": period_label,
        "目標摂取kcal": int(target),
        "経過日数": f"{elapsed_days}日",
        "記録日数": f"{logged_days}日",
        "記録率": f"{rate}%",
        "月間摂取合計kcal": int(total_intake),
        "記録日あたりの平均摂取kcal": f"{_f(avg)}kcal",
        "月平均の摂取達成率": f"{avg_pct}%",
        "月間の運動消費kcal": int(total_burn),
        "月間の赤字合計kcal": int(entries_deficit),
        "体脂肪換算": f"約{round(entries_deficit / FAT_KCAL_PER_KG, 2)}kg分",
    }
    if wd is not None:
        facts["月初の体重kg"] = _f(weight_start)
        facts["月末の体重kg"] = _f(weight_end)
        facts["体重変化"] = f"{'+' if wd > 0 else ''}{wd}kg（{pct}%）"
    else:
        facts["体重変化"] = "未記録"
    return facts


def _render_facts(facts: dict) -> str:
    return "\n".join(f"- {k}: {v}" for k, v in facts.items())


def _build_system(persona: Optional[dict]) -> str:
    p = dict(DEFAULT_PERSONA)
    if persona:
        for k in p:
            if persona.get(k):
                p[k] = persona[k]
    return COMMON_RULES + PERSONA_BLOCK.format(bot_name=p["bot_name"], pronoun=p["pronoun"], tone=p["tone"])


def _strip_commas(text: str) -> str:
    """7,560 → 7560 のように桁区切りカンマを除去（誤検知防止）。"""
    return re.sub(r"(?<=\d),(?=\d{3})", "", text)


def _known_numbers(text: str) -> set:
    known = set()
    for tok in re.findall(r"\d+(?:\.\d+)?", _strip_commas(text)):
        try:
            f = float(tok)
        except ValueError:
            continue
        known.add(f"{f:.1f}")
        known.add(str(int(f)) if f == int(f) else f"{f:.1f}")
    return known


def _norm_num(tok: str) -> str:
    f = float(tok)
    return str(int(f)) if f == int(f) else f"{f:.1f}"


def _parse(raw: str) -> dict:
    t = re.sub(r"```(?:json)?", "", raw.strip()).strip()
    m = re.search(r"\{.*\}", t, flags=re.S)
    if not m:
        raise ValueError("JSON object not found in response")
    return json.loads(m.group(0))


def _validate(d: dict, allowed: set) -> None:
    for k in ("headline", "comment", "advice"):
        if not isinstance(d.get(k), str) or not d[k].strip():
            raise ValueError(f"missing/empty field: {k}")
        if len(d[k]) > LIMITS[k]:
            raise ValueError(f"{k} too long ({len(d[k])} > {LIMITS[k]})")
        for ng in FORBIDDEN:
            if ng in d[k]:
                raise ValueError(f"forbidden expression in {k}: {ng}")
    joined = _strip_commas(" ".join(d[k] for k in ("headline", "comment", "advice")))
    allowed = set(allowed) | {"7000", "7200", "7.0", "7.2"}
    invented = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(kcal|kg|%|g)?", joined):
        val, unit = m.group(1), m.group(2)
        if unit is None:
            try:
                if float(val) < 100:
                    continue
            except ValueError:
                continue
        if _norm_num(val) not in allowed:
            invented.append(val + (unit or ""))
    if invented:
        raise ValueError(f"invented numbers: {sorted(set(invented))}")


def _rule_fallback(scope: str, facts: dict) -> dict:
    from app.handlers import report_comment as rules
    return rules.ai_fallback(scope, facts)


def _post_gemini(user_text: str, system_text: str) -> str:
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is not set")
    payload = {
        "systemInstruction": {"parts": [{"text": system_text}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "temperature": TEMPERATURE,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    last = None
    for model in MODELS:
        url = f"{API_BASE}/{model}:generateContent?key={GEMINI_API_KEY}"
        try:
            r = httpx.post(url, json=payload, timeout=TIMEOUT_SEC)
            if r.status_code == 404:
                last = f"{model}:404"
                continue
            r.raise_for_status()
            cand = (r.json().get("candidates") or [{}])[0]
            parts = (cand.get("content") or {}).get("parts") or []
            text = "".join(p.get("text", "") for p in parts).strip()
            if len(text) < 5:
                last = f"{model}:empty"
                continue
            return text
        except Exception as e:  # noqa: BLE001
            last = f"{model}:{type(e).__name__}"
            logger.warning("report_ai model %s failed: %s", model, e)
    raise RuntimeError(f"all models failed: {last}")


def _cache_key(scope: str, user_id: str, period_key: str, facts_text: str, persona: Optional[dict]) -> str:
    raw = f"{scope}|{user_id}|{period_key}|{facts_text}|{json.dumps(persona or {}, sort_keys=True, ensure_ascii=False)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[dict]:
    try:
        from app.services import db
        return db.get_report_comment(key)
    except Exception:
        return None


def _cache_set(key: str, scope: str, user_id: str, period_key: str, result: dict, facts_text: str) -> None:
    try:
        from app.services import db
        db.save_report_comment(key, user_id, scope, period_key,
                               result["headline"], result["comment"], result["advice"],
                               result["source"], facts_text)
    except Exception as e:  # noqa: BLE001
        logger.warning("report_comment cache save skipped: %s", e)


def generate(scope: str, facts: dict, *, persona: Optional[dict] = None, user_id: str = "",
             period_key: str = "", use_cache: bool = True) -> dict:
    facts_text = _render_facts(facts)
    allowed = _known_numbers(facts_text)
    key = _cache_key(scope, user_id, period_key, facts_text, persona)

    if use_cache:
        cached = _cache_get(key)
        if cached:
            return {**cached, "source": "cache", "cache_key": key}

    user_text = TEMPLATES[scope].format(facts=facts_text, fat_note=FAT_NOTE)
    try:
        data = _parse(_post_gemini(user_text, _build_system(persona)))
        _validate(data, allowed)
        result = {"headline": data["headline"].strip(),
                  "comment": data["comment"].strip(),
                  "advice": data["advice"].strip(),
                  "source": "ai"}
    except Exception as e:  # noqa: BLE001
        logger.warning("report_ai fallback (%s): %s", scope, e)
        result = {**_rule_fallback(scope, facts), "source": "rule"}

    if use_cache:
        _cache_set(key, scope, user_id, period_key, result, facts_text)
    return {**result, "cache_key": key}


def generate_daily(facts: dict, **kw) -> dict:
    return generate("daily", facts, **kw)


def generate_weekly(facts: dict, **kw) -> dict:
    return generate("weekly", facts, **kw)


def generate_monthly(facts: dict, **kw) -> dict:
    return generate("monthly", facts, **kw)
