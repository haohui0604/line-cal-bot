"""Gemini を使った自由文解析・アドバイス生成 (同期版).

用途:
  - 「さっきラーメン食べた」→ 栄養推定 (intent=record)
  - 「今日あとどれくらい食べていい？」→ 残り予算アドバイス (intent=question)
  - 雑談・リアクション (intent=chat)

DBには書き込まない。書き込みは呼び出し側 (text_handler) の責務。
"""
import json
import logging
import re

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MODEL = "gemini-3.5-flash"

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


def chat(user_message: str, context: dict) -> dict:
    """Gemini へ自由文を投げ、構造化 dict を返す。失敗時は例外。"""
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent?key={api_key}"
    )
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
    with httpx.Client(timeout=30.0) as cli:
        r = cli.post(url, json=payload)
        r.raise_for_status()
        body = r.json()

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
             "food_name": "食パン", "quantity_g": 100.0 or None}, ...]
    戻り値: items と同じ並びの list。各要素に kcal/protein_g/fat_g/
            carb_g/salt_g が補完される。失敗時は例外。
    """
    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    lines = ["以下の食事記録の各行について、日本の一般的な食品成分値で"
             "栄養を推定してください。\n"]
    for i, it in enumerate(items):
        q = f"{it['quantity_g']:.0f}g" if it.get("quantity_g") else "1人前"
        lines.append(f"{i}. {it['food_name']} ({q})")
    lines.append(
        "\n必ず次のJSONだけを返してください。説明文・コードフェンスは不要。\n"
        '{"foods": [{"index": 0, "kcal": 数値, "protein_g": 数値, '
        '"fat_g": 数値, "carb_g": 数値, "salt_g": 数値}, ...]}\n'
        "ルール:\n"
        "- index は入力行番号と一致させる\n"
        "- 推定が困難でも妥当な中央値で必ず数値を入れる\n"
        "- 分量が指定されている場合はその分量で計算する"
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": "\n".join(lines)}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }
    last_exc = None
    import time as _time
    for attempt in range(3):
        try:
            with httpx.Client(timeout=60.0) as cli:
                r = cli.post(url, json=payload)
                if r.status_code in (429, 503):
                    _time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                body = r.json()
            raw = body["candidates"][0]["content"]["parts"][0]["text"]
            data = parse_llm_json(raw)
            for f in data.get("foods", []):
                idx = f.get("index")
                if isinstance(idx, int) and 0 <= idx < len(items):
                    for key in ("kcal", "protein_g", "fat_g",
                                "carb_g", "salt_g"):
                        if f.get(key) is not None:
                            items[idx][key] = float(f[key])
            return items
        except httpx.HTTPStatusError as e:
            last_exc = e
            if e.response.status_code not in (429, 503):
                raise
            _time.sleep(2 * (attempt + 1))
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            last_exc = e
            _time.sleep(2 * (attempt + 1))
    raise last_exc or RuntimeError("batch estimate retry exhausted")
