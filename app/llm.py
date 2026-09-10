"""Gemini を使った自由文解析・アドバイス生成 (同期版).

用途:
  - 「さっきラーメン食べた」→ 栄養推定 (intent=record)
  - 「今日あとどれくらい食べていい？」→ 残り予算アドバイス (intent=question)
  - 雑談・リアクション (intent=chat)
  - 一括登録時の kcal 未記載行のまとめ推定

DBには書き込まない。書き込みは呼び出し側 (text_handler) の責務。
"""
import json
import logging
import re
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# モデル名のフォールバック順 (新しい/軽量/旧世代)
# Render側で503や404が出たときに次候補へ自動切替。
MODEL_PRIMARY = "gemini-flash-latest"
MODEL_FALLBACKS = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

SYSTEM_PROMPT = """\
あなたはLINEのカロリー管理パートナー「おがさのカロリー収支管理」です。
ユーザーの食事記録とダイエット継続を、明るく前向きな一言で支えます。

必ず次のJSONだけを返してください。説明文・コードフェンス(```)は不要です。

{
  "intent": "record" | "question" | "chat",
  "reaction": "食事への短い自然なリアクション (例: うまそう！ / たんぱく質しっかり取れてていいね) 40字以内",
  "foods": [
    {
      "name": "食品名",
      "kcal": 数値,
      "protein_g": 数値,
      "fat_g": 数値,
      "carb_g": 数値,
      "salt_g": 数値,
      "quantity_g": 数値またはnull
    }
  ],
  "meal_slot": "breakfast" | "lunch" | "dinner" | "snack" | null,
  "answer": "質問・雑談への回答 120字以内"
}

ルール:
- 食事の報告(食べた/飲んだ/これから食べる等)は intent=record。foods は1品ずつ分解し、日本の一般的な食品成分で推定する。推定が困難でも妥当な中央値で必ず数値を入れる。
- 「あとどれくらい食べていい」「足りてる？」等の相談は intent=question。ユーザーの今日の摂取状況を踏まえ、answer に具体的な数値と次の一手を入れる。
- 挨拶・雑談は intent=chat。
- reaction は絵文字1個まで。ポジティブに、ただし脂質・塩分が明らかに過多なときは一言だけ優しく注意を添える。
- 数値は推定であることを前提に、断定的すぎない表現にする。
"""


def _build_user_message(user_message: str, context: dict) -> str:
    lines = [
        "【ユーザーの発言】",
        user_message,
        "",
        "【今日の状況 (コンテキスト)】",
        f"摂取: {context.get('intake_kcal', 0):.0f} kcal",
        f"消費: {context.get('burn_kcal', 0):.0f} kcal",
        f"目標摂取: {context.get('target_kcal', 1800):.0f} kcal "
        f"(残り {context.get('remaining_kcal', 0):.0f} kcal)",
        f"タンパク質: {context.get('protein_g', 0):.0f} g "
        f"(目標 {context.get('protein_target_g', 100):.0f} g, "
        f"残り {context.get('remaining_protein_g', 0):.0f} g)",
        f"食塩: {context.get('salt_g', 0):.2f} g (目安 7.5 g 未満)",
    ]
    today_foods = context.get("today_foods") or []
    if today_foods:
        lines.append("今日の記録済み: " + " / ".join(today_foods[:10]))
    return "\n".join(lines)


def _post_with_fallback(payload: dict, timeout: float = 30.0) -> dict:
    """MODEL_PRIMARY → FALLBACKS の順に試し、最初に成功したものを返す.

    429 / 503 / ReadTimeout は次のモデル候補へ。404 はそのモデル名が存在しないため次へ。
    """
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    models = [MODEL_PRIMARY] + [m for m in MODEL_FALLBACKS if m != MODEL_PRIMARY]
    last_exc = None
    for model in models:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={api_key}"
        )
        try:
            with httpx.Client(timeout=timeout) as cli:
                r = cli.post(url, json=payload)
                if r.status_code == 404:
                    logger.warning("Gemini model %s 404 → next", model)
                    last_exc = RuntimeError(f"model {model} not found")
                    continue
                if r.status_code in (429, 503):
                    logger.warning("Gemini model %s %s → next",
                                   model, r.status_code)
                    last_exc = RuntimeError(f"{model} busy {r.status_code}")
                    continue
                r.raise_for_status()
                return r.json()
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            logger.warning("Gemini model %s timeout → next", model)
            last_exc = e
            continue
    raise last_exc or RuntimeError("all Gemini models failed")


def chat(user_message: str, context: dict) -> dict:
    """Gemini へ自由文を投げ、構造化 dict を返す。失敗時は例外."""
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{
            "parts": [{"text": _build_user_message(user_message, context)}],
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.4,
        },
    }
    body = _post_with_fallback(payload, timeout=30.0)
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    return parse_llm_json(raw)


def parse_llm_json(raw: str) -> dict:
    """LLM応答からJSONを頑健に取り出す."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE)
    m = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if m:
        text = m.group(0)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM response is not a JSON object")
    data.setdefault("intent", "chat")
    data.setdefault("reaction", "")
    data.setdefault("foods", [])
    data.setdefault("answer", "")
    return data


def estimate_foods_batch(items: list) -> list:
    """一括登録モード用: kcal未記載の行をまとめて栄養推定する.

    items: [{"date": "2026-09-01", "meal_slot": "breakfast",
             "food_name": "食パン", "quantity_g": 100.0 or None, "kcal": 0.0}, ...]
    戻り値: items と同じ並び。各要素に kcal/protein_g/fat_g/
            carb_g/salt_g が補完される。1件でも推定失敗したら例外を投げ、
            呼び出し側で per-item 個別推定にフォールバックする。
    """
    lines = ["以下の食事記録の各行について、日本の一般的な食品成分値で"
             "栄養を推定してください。\n"]
    for i, it in enumerate(items):
        q = f"{it.get('quantity_g', 0):.0f}g" if it.get("quantity_g") else "1人前"
        lines.append(f"{i}. {it['food_name']} ({q})")
    lines.append(
        "\n必ず次のJSONだけを返してください。説明文・コードフェンスは不要。\n"
        '{"foods": [{"index": 0, "kcal": 数値, "protein_g": 数値, '
        '"fat_g": 数値, "carb_g": 数値, "salt_g": 数値}, ...]}\n'
        "ルール:\n"
        "- index は入力行番号と一致させる\n"
        "- 推定が困難でも妥当な中央値で必ず数値を入れる (kcal だけでも OK)\n"
        "- 分量が指定されている場合はその分量で計算する"
    )
    payload = {
        "contents": [{"parts": [{"text": "\n".join(lines)}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }
    body = _post_with_fallback(payload, timeout=60.0)
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    data = parse_llm_json(raw)
    for f in data.get("foods", []):
        idx = f.get("index")
        if isinstance(idx, int) and 0 <= idx < len(items):
            kcal = f.get("kcal")
            if kcal is not None:
                items[idx]["kcal"] = float(kcal)
            for key in ("protein_g", "fat_g", "carb_g", "salt_g"):
                if f.get(key) is not None:
                    items[idx][key] = float(f[key])
    missing = [i for i, it in enumerate(items) if (it.get("kcal") or 0.0) <= 0]
    if missing:
        raise ValueError(f"estimate missing for indices: {missing}")
    return items


def estimate_food_single(item: dict) -> dict:
    """1件だけの栄養推定 (batch が落ちたときの per-item フォールバック)."""
    payload = {
        "contents": [{
            "parts": [{
                "text": (
                    f"「{item['food_name']}」({item.get('quantity_g')}g or 1人前) "
                    "の日本の一般的な栄養成分を推定し、次のJSONのみ返してください。"
                    '{"kcal": 数値, "protein_g": 数値, "fat_g": 数値, '
                    '"carb_g": 数値, "salt_g": 数値}'
                )
            }],
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }
    body = _post_with_fallback(payload, timeout=30.0)
    raw = body["candidates"][0]["content"]["parts"][0]["text"]
    data = parse_llm_json(raw)
    for k in ("kcal", "protein_g", "fat_g", "carb_g", "salt_g"):
        if data.get(k) is not None:
            item[k] = float(data[k])
    return item
