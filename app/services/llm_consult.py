"""相談（あと何食べていい？）と「同じ食事」参照抽出のための LLM 呼び出し.

llm.py とは分離し、相談では推論力の高いモデルを優先する。
数値の決定・検算は呼び出し側（consult.py / repeat_meal.py）の責務とし、
ここでは「解釈」と「文言生成」だけを行う。
"""
import logging

import httpx

from app.config import settings
from app.services.llm import parse_llm_json, PERSONA_DEFAULT

logger = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# 相談用: まず高推論モデル → 失敗したら軽量モデルへ
MODEL_CONSULT = "gemini-2.5-pro"
MODEL_CONSULT_FALLBACKS = ["gemini-2.5-flash", "gemini-flash-latest"]

# 「同じ食事」参照抽出用: 軽量・高速で十分
MODEL_EXTRACT = "gemini-flash-latest"
MODEL_EXTRACT_FALLBACKS = ["gemini-flash-lite-latest"]


def _post(models, payload: dict, timeout: float = 20.0) -> dict:
    """モデル候補を順に試し、最初に成功した応答を返す.

    404 / 429 / 503 / タイムアウトは次のモデルへフォールバックする。
    """
    key = settings.GEMINI_API_KEY
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set")

    last = None
    for model in models:
        url = f"{API_BASE}/{model}:generateContent?key={key}"
        try:
            with httpx.Client(timeout=timeout) as cli:
                r = cli.post(url, json=payload)
                if r.status_code == 404:
                    logger.warning("gemini %s 404 → next", model)
                    last = RuntimeError(f"{model} not found")
                    continue
                if r.status_code in (429, 503):
                    logger.warning("gemini %s %s → next", model, r.status_code)
                    last = RuntimeError(f"{model} busy {r.status_code}")
                    continue
                r.raise_for_status()
                return r.json()
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            logger.warning("gemini %s timeout → next", model)
            last = e
            continue
    raise last or RuntimeError("all gemini models failed")


def _text_of(body: dict) -> str:
    parts = (body.get("candidates") or [{}])[0].get("content", {}).get("parts") or []
    return parts[0].get("text", "") if parts else ""


# ---------- 「いついつの◯◯と同じ」の参照抽出 ----------

EXTRACT_SYSTEM = """\
あなたはLINEの食事記録アシスタントです。ユーザーの発言から
「過去の食事をそのまま複製したい」という意図だけを構造化して抽出します。
日付の計算はしません（日付計算はコードが行います）。日付は表現のまま写してください。

必ず次のJSONだけを返してください。
{"is_repeat": true|false,
 "date_expr": "昨日" | "おととい" | "先週の火曜" | "10/2" | "2026-10-02" | null,
 "meal": "breakfast" | "lunch" | "dinner" | "snack" | null}

ルール:
- 「昨日の昼と同じ」「おとといの夕食と一緒で」「先週の火曜の昼と同じものを」は is_repeat=true。
- date_expr は発言中の日付表現をそのまま入れる。勝手に今日・昨日と決めない。無ければ null。
- meal は 朝食=breakfast / 昼食=lunch / 夕食=dinner / 間食・夜食=snack。無ければ null。
- 「同じくらい食べた」のような食事の報告や、複製と無関係な発言は is_repeat=false。
"""


def extract_meal_ref(user_text: str) -> dict:
    """「同じ」参照の構造化抽出（日付解決はしない）."""
    payload = {
        "system_instruction": {"parts": [{"text": EXTRACT_SYSTEM}]},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.0,
            "maxOutputTokens": 512,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    body = _post([MODEL_EXTRACT] + MODEL_EXTRACT_FALLBACKS, payload, timeout=8.0)
    return parse_llm_json(_text_of(body))


# ---------- 「あと何食べていい？」相談 ----------

CONSULT_SYSTEM = """\
あなたは管理栄養士レベルの献立提案を行うアシスタントです。
与えられた【今日の状況】【過去の傾向】【目標】だけを根拠に、
「今から食べるとよい具体的な献立」を2〜3案つくります。

必ず守ること:
- 各案は具体的な食品名と分量（g / 個 / 杯）を必ず入れる。「焼き魚」「豆腐」だけで終わらせない。
  例: 「鮭の塩焼き 80g」「木綿豆腐 150g」「納豆 1パック」。
- 各食品に kcal・protein_g・fat_g・carb_g・salt_g を入れる（日本の一般的な成分値）。
- 各案の合計 kcal は【残り予算】を超えないこと。超えそうなら量を減らす。
- 【過去の傾向】でよく食べている食品を1つ以上取り入れる。
- why には、その案が「なぜ今のあなたに合うか」を、残りPFC・不足栄養素・普段の食事に
  触れて1〜2文で書く。
- 断定・医療的助言は禁止。数値は目安であることを前提にする。

必ず次のJSONだけを返してください。
{"intro": "80字以内の前置き",
 "options": [{"title": "案の名前(20字以内)",
              "items": [{"name": "食品名", "quantity": "150g", "kcal": 0,
                         "protein_g": 0, "fat_g": 0, "carb_g": 0, "salt_g": 0}],
              "why": "120字以内"}],
 "notes": "注意点があれば120字以内"}
"""


def _consult_user_message(user_text: str, ctx: dict) -> str:
    lines = ["【ユーザーの発言】", user_text, "",
             "【今日の状況（コードで計算済み。この数値を使う）】"]
    lines.append(f"日付: {ctx.get('today')}")
    lines.append(f"目標摂取: {float(ctx.get('target') or 0):.0f} kcal")
    lines.append(f"今日の摂取: {float(ctx.get('intake') or 0):.0f} kcal"
                 f" / 消費: {float(ctx.get('burn') or 0):.0f} kcal")
    lines.append(f"【残り予算】: {float(ctx.get('budget_kcal') or 0):.0f} kcal"
                 "（案の合計はこれを超えないこと）")
    rp = ctx.get("remaining_pfc") or {}
    if rp:
        parts = []
        for label, key in (("たんぱく質", "protein_g"), ("脂質", "fat_g"),
                           ("炭水化物", "carb_g")):
            v = rp.get(key)
            if v is not None:
                parts.append(f"{label} 残り {float(v):.0f}g")
        if parts:
            lines.append("残りPFC: " + " / ".join(parts))
    tf = ctx.get("today_foods") or []
    lines.append("今日すでに食べた: " + ("、".join(tf) or "なし"))

    lines += ["", "【過去の傾向（実データ）】"]
    avg = ctx.get("avg_intake")
    if avg:
        lines.append(f"直近2週間の平均摂取: {float(avg):.0f} kcal/日")
    ff = ctx.get("frequent_foods") or []
    if ff:
        lines.append("よく食べている食品: " + "、".join(
            f"{f['name']}({f['count']}回)" for f in ff[:10]))
    sc = ctx.get("slot_counts") or {}
    if sc:
        jp = {"breakfast": "朝食", "lunch": "昼食", "dinner": "夕食", "snack": "間食"}
        lines.append("記録の多い食事区分: " + "、".join(
            f"{jp.get(k, k)}{v}件" for k, v in sorted(sc.items(), key=lambda x: -x[1])))
    if ctx.get("goal_line"):
        lines.append("目標: " + str(ctx["goal_line"]))
    if ctx.get("weight"):
        lines.append(f"現在の体重: {ctx['weight']}kg")
    lines.append(f"食塩の残り目安: {float(ctx.get('salt_remaining') or 0):.1f}g")
    if ctx.get("considered"):
        lines.append("【ユーザーが検討中のメニュー】" + str(ctx["considered"]))
    if ctx.get("constraint"):
        lines.append("【追加の要望】" + str(ctx["constraint"]))
    lines.append("")
    lines.append("上記のデータに基づき、今食べるなら何をどれだけ食べるとよいか、"
                 "具体的な献立を2〜3案、指定のJSONで返してください。")
    return "\n".join(lines)


def consult_advice(user_text: str, ctx: dict, persona: dict | None = None) -> dict:
    """相談への献立提案（構造化JSON）。数値の検算は呼び出し側で行う."""
    p = {**PERSONA_DEFAULT, **(persona or {})}
    tone = str(p.get("bot_tone") or "").strip() or "明るく前向き。敬体と砕けた口調の中間。"
    sysmsg = (f"あなたの名前は「{str(p.get('bot_name'))[:12]}」。"
              f"一人称は「{str(p.get('bot_pronoun'))[:6]}」で統一。"
              f"性格・口調: {tone[:100]}。\n" + CONSULT_SYSTEM)
    payload = {
        "system_instruction": {"parts": [{"text": sysmsg}]},
        "contents": [{"parts": [{"text": _consult_user_message(user_text, ctx)}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.6,
            "maxOutputTokens": 3072,
        },
    }
    body = _post([MODEL_CONSULT] + MODEL_CONSULT_FALLBACKS, payload, timeout=25.0)
    return parse_llm_json(_text_of(body))


# ---------- 「これ食べたらどう？」の品目抽出 ----------

PROPOSAL_EXTRACT_SYSTEM = """\
ユーザーの発言から「これから食べようとしている食品」を抽出します。
栄養値の推定はしません（栄養は別の処理が行います）。

必ず次のJSONだけを返してください。
{"ate_already": true|false,
 "foods": [{"name": "食品名", "quantity": "1杯", "quantity_g": 数値またはnull}]}

ルール:
- 「ラーメン食べたらどう？」「唐揚げ5個はアリ？」「夜はカレーにしようかな」
  → ate_already=false、foods に品目を入れる。
- 複数品目は分けて入れる（「ラーメンと餃子2個」→ 2件）。
- 「2個」「大盛り」「1杯」「150g」など量が読み取れるときは quantity に入れ、
  g換算できるときは quantity_g に数値（不明なら null）。
- 「ラーメン食べた」「昼に牛丼食べちゃった」のように【すでに食べた】報告は
  ate_already=true とし、foods にはその品目を入れる。
- 食品が読み取れない発言は foods を空配列にする。
"""


def extract_proposal_foods(user_text: str) -> dict:
    """提案発言から品目と量を抽出する（栄養値は含めない）."""
    payload = {
        "system_instruction": {"parts": [{"text": PROPOSAL_EXTRACT_SYSTEM}]},
        "contents": [{"parts": [{"text": user_text}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.0,
            "maxOutputTokens": 512,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    body = _post([MODEL_EXTRACT] + MODEL_EXTRACT_FALLBACKS, payload, timeout=10.0)
    return parse_llm_json(_text_of(body))


# ---------- 提案の評価に対するAIの一言 ----------

PROPOSAL_VERDICT_SYSTEM = """\
あなたは管理栄養士レベルのアドバイザーです。すでに計算済みの数値が与えられます。
数値を再計算したり変更したりしてはいけません。与えられた数値だけを根拠にします。

返すもの:
- verdict: 提案への短い判定（80字以内）。オーバー／ぴったり／余裕を踏まえ、
  たんぱく質や脂質・食塩の過不足にも一言触れる。断定・説教はしない。
- adjustments: 食べ方を少し変えて収める具体案を1〜2件。
  label は「ごはん少なめ」「味噌汁→わかめスープ」のような短い操作、
  detail は補足（40字以内）、save_kcal は減るkcalの目安（数値）。
- notes: 注意点があれば60字以内（無ければ空文字）。

必ず次のJSONだけを返してください。
{"verdict": "...", "adjustments": [{"label": "...", "detail": "...",
 "save_kcal": 数値}], "notes": "..."}
"""


def _proposal_user_message(user_text: str, ctx: dict, verdict: dict) -> str:
    t = verdict.get("totals") or {}
    lines = ["【ユーザーの発言】", user_text, "",
             "【計算済みの数値（この値だけを使う。再計算禁止）】"]
    lines.append(f"提案の合計: {t.get('kcal', 0):.0f} kcal / "
                 f"P{t.get('protein_g', 0):.0f} F{t.get('fat_g', 0):.0f} "
                 f"C{t.get('carb_g', 0):.0f} 食塩{t.get('salt_g', 0):.1f}g")
    lines.append(f"今日の摂取: {verdict.get('intake', 0):.0f} kcal"
                 f" → 足すと {verdict.get('after_intake', 0):.0f} kcal")
    lines.append(f"1日の目標: {verdict.get('target', 0):.0f} kcal")
    lines.append(f"今日の残り: {verdict.get('remaining', 0):.0f} kcal"
                 f" / 提案との差: {verdict.get('diff', 0):+.0f} kcal")
    ps = verdict.get("protein_short")
    if ps is not None:
        lines.append(f"たんぱく質: 目標 {verdict.get('protein_target', 0):.0f}g に対して"
                     f" {'不足' if ps > 0 else '超過'} {abs(ps):.0f}g")
    fs = verdict.get("fat_short")
    if fs is not None:
        lines.append(f"脂質: 目標 {verdict.get('fat_target', 0):.0f}g に対して"
                     f" {'不足' if fs > 0 else '超過'} {abs(fs):.0f}g")
    if verdict.get("salt_over") is not None:
        lines.append(f"食塩: 残り目安との差 {verdict['salt_over']:+.1f}g")
    flags = verdict.get("flags") or []
    lines.append("判定フラグ: " + ("、".join(flags) if flags else "問題なし"))
    ff = ctx.get("frequent_foods") or []
    if ff:
        lines.append("よく食べている食品: " + "、".join(f["name"] for f in ff[:8]))
    if ctx.get("weight"):
        lines.append(f"現在の体重: {ctx['weight']}kg")
    lines += ["", "上記の数値だけを根拠に、判定と調整案を指定のJSONで返してください。"]
    return "\n".join(lines)


def phrase_proposal(user_text: str, ctx: dict, verdict: dict,
                    persona: dict | None = None) -> dict:
    """計算済みの過不足を、ユーザー向けの文言にする（数値は変更させない）."""
    p = {**PERSONA_DEFAULT, **(persona or {})}
    tone = str(p.get("bot_tone") or "").strip() or "明るく前向き。敬体と砕けた口調の中間。"
    sysmsg = (f"あなたの名前は「{str(p.get('bot_name'))[:12]}」。"
              f"一人称は「{str(p.get('bot_pronoun'))[:6]}」で統一。"
              f"性格・口調: {tone[:100]}。\n" + PROPOSAL_VERDICT_SYSTEM)
    payload = {
        "system_instruction": {"parts": [{"text": sysmsg}]},
        "contents": [{"parts": [{"text": _proposal_user_message(user_text, ctx, verdict)}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.5,
            "maxOutputTokens": 1536,
        },
    }
    body = _post([MODEL_CONSULT] + MODEL_CONSULT_FALLBACKS, payload, timeout=20.0)
    return parse_llm_json(_text_of(body))
