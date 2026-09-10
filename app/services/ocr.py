"""Gemini マルチモーダル解析 (成分表OCR + 食べ物写真の推定).

- 栄養成分表の写真 → 数値を厳密に読み取り (mode=label)
- 料理・食べ物の写真 → 見た目から品目と栄養を推定 (mode=photo)

呼び出し側 (image_handler) は同期なので、本モジュールも同期実装。
"""
import base64
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MODEL = "gemini-flash-latest"

OCR_PROMPT = """\
あなたは食事画像の解析アシスタントです。送信された画像は
(A) 食品パッケージの栄養成分表、または (B) 料理・食べ物そのもの のどちらかです。

まず画像がどちらかを判断し、以下のJSONだけを返してください
(説明文・コードフェンスは不要)。

{
  "mode": "label" | "photo",
  "name": "食品名または商品名",
  "kcal": 数値,
  "protein_g": 数値,
  "fat_g": 数値,
  "carb_g": 数値,
  "salt_g": 数値,
  "quantity_g": 数値またはnull,
  "brand": "ブランド名 (labelの場合のみ。不明ならnull)",
  "confidence": "confirmed" | "estimated",
  "reaction": "食事への短いポジティブな一言 (photoの場合のみ、40字以内)"
}

ルール:
- (A) 成分表の場合: mode=label, confidence=confirmed。
  表記単位(100g当たり/1食当たり/1個当たり)を厳密に守って数値を読み取る。
  1食当たり表記なら quantity_g にその量が分かれば入れる。
  読み取れない項目は null。
- (B) 食べ物の写真の場合: mode=photo, confidence=estimated。
  写っている料理を具体的に特定し(例: 「天ぷらうどん」)、
  日本の一般的な食品成分値で妥当な中央値を必ず数値で入れる。
  quantity_g は一般的な1人前の量。
  brand は null。
- 食事に無関係な画像の場合: mode=photo, name="不明", kcal=0 として返す。
"""


def extract_label(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Gemini へ画像解析を依頼し、レスポンス本文(JSON文字列)を返す (同期版).

    503/429 は最大3回リトライ。関数名は後方互換のため extract_label のまま。
    """
    if settings.OCR_BACKEND != "gemini":
        raise NotImplementedError(
            f"OCR back-end {settings.OCR_BACKEND} not implemented"
        )

    api_key = settings.GEMINI_API_KEY
      try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        if max(img.size) > 1280:
            ratio = 1280 / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)))
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            image_bytes = buf.getvalue()
    except Exception:
        pass  # 縮小失敗時は元画像で続行
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": OCR_PROMPT},
                {"inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                }},
            ],
        }],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.2,
        },
    }

    last_exc = None
    for attempt in range(3):
        try:
            with httpx.Client(timeout=180.0) as cli:
                r = cli.post(url, json=payload)
                if r.status_code in (429, 503):
                    time.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                body = r.json()
            return body["candidates"][0]["content"]["parts"][0]["text"]
        except httpx.HTTPStatusError as e:
            last_exc = e
            if e.response.status_code not in (429, 503):
                raise
            time.sleep(2 * (attempt + 1))
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            last_exc = e
            time.sleep(2 * (attempt + 1))
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            last_exc = e
            time.sleep(3 * (attempt + 1))

    raise last_exc or RuntimeError("Gemini image API retry exhausted")
  

