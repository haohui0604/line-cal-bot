"""Gemini 2.0 Flash へのマルチモーダル OCR 依頼."""
import base64
import logging
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

OCR_PROMPT = """\
あなたは栄養成分ラベル読取アシスタントです。
画像内の栄養成分表示の数値を厳密に読み取り、以下の JSON 形式で返してください。
    {
      "name": "商品名(不明なら null)",
      "kcal": 数値(エネルギー/熱量 kcal/100g当たり または1食当たりどちらか明確に),
      "protein_g": 数値,
      "fat_g": 数値,
      "carb_g": 数値,
      "salt_g": 食塩相当量(数値),
      "quantity_g": 1食分のグラム数(数値, 不明なら null),
      "brand": "商品名/ブランド(不明なら null)"
    }
該当する値が画像内で見つからない項目は null を返してください。
数値以外にも単位が書いたもの(例: µg, mg)は信頼できる単位で記載。
JSON以外の説明は不要。"""


async def extract_label(image_bytes: bytes) -> str:
    """Gemini 2.0 Flash へ OCR。レスポンス本文(JSON文字列)を返す."""
    if settings.OCR_BACKEND != "gemini":
        raise NotImplementedError(f"OCR back-end {settings.OCR_BACKEND} not implemented")

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": OCR_PROMPT},
                {"inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ],
        }],
        "generationConfig": {"response_mime_type": "application/json", "temperature": 0.0},
    }
    async with httpx.AsyncClient(timeout=30.0) as cli:
        r = await cli.post(url, json=payload)
        r.raise_for_status()
        body = r.json()
    return body["candidates"][0]["content"]["parts"][0]["text"]
