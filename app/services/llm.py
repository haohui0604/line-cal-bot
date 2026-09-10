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

MODEL = "gemini-2.0-flash"

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
