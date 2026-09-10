"""Gemini マルチモーダル解析 (成分表OCR + 食べ物写真の推定 + 体重計OCR).

- 栄養成分表の写真 → 数値を厳密に読み取り (mode=label)
- 料理・食べ物の写真 → 見た目から品目と栄養を推定 (mode=photo)
- 体重計・体組成計の計測表示写真 → 体重/体脂肪/筋肉量/BMR を読み取り (mode=weight)

呼び出し側 (image_handler) は同期なので、本モジュールも同期実装。
"""
import base64
import logging
import time

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MODEL = "gemini-flash-lite-latest"

OCR_PROMPT = """\
あなたは食事または健康計測画像の解析アシスタントです。送信された画像は
(A) 食品パッケージの栄養成分表
(B) 料理・食べ物そのもの
(C) 体重計・体組成計の計測表示 (数字が乗った画面)
のいずれかです。

まず画像がどれかを判断し、以下のJSONだけを返してください
(説明文・コードフェンスは不要)。

{
  "mode": "label" | "photo" | "weight",

  // (A)(B) で使用
  "name": "食品名または商品名 (label/photo) / null (weight)",
  "kcal": 数値 (label/photo) / null (weight),
  "protein_g": 数値 (label/photo) / null (weight),
  "fat_g": 数値 (label/photo) / null (weight),
  "carb_g": 数値 (label/photo) / null (weight),
  "salt_g": 数値 (label/photo) / null (weight),
  "quantity_g": 数値またはnull,
  "brand": "ブランド名 (label) / null",

  // (C) で使用
  "weight_kg": 数値 (weight) / null (label/photo),
  "body_fat_pct": 数値またはnull,
  "muscle_kg": 数値またはnull,
  "bmr_kcal": 数値またはnull,

  "confidence": "confirmed" | "estimated",
  "reaction": "短いポジティブな一言 (photo/weight の場合のみ、40字以内)"
}

ルール:
- (A) 成分表: mode=label, confidence=confirmed。
  表記単位 (100g 当たり / 1食当たり / 1個当たり) を厳密に守る。
  1食当たり表記なら quantity_g にその量を入れる。読み取れない項目は null。
- (B) 料理写真: mode=photo, confidence=estimated。
  料理を具体的に特定し (例: 「天ぷらうどん」)、日本の一般的な食品成分値で
  妥当な中央値を必ず数値で入れる。quantity_g は一般的な 1人前の量。brand は null。
- (C) 体重計: mode=weight, confidence=confirmed (実機計測のため)。
  表示されている数値 (kg) を厳密に読み取る。体脂肪率・筋肉量・基礎代謝が
  表示されていればそれぞれの数値を入れる。kcal 関連の項目は全て null。
  reaction にポジティブな一言を入れる。
- 食事や健康計測に無関係な画像: mode=photo, name="不明", kcal=0,
  weight_kg=null として返す。
"""


def extract_label(image_bytes: bytes, mime_type: str = "image/jpeg") -> str:
    """Gemini へ画像解析を依頼し、レスポンス本文 (JSON文字列) を返す (同期版).

    503/429/ReadTimeout は最大3回リトライ。関数名は後方互換のため extract_label のまま。
    """
    if settings.OCR_BACKEND != "gemini":
        raise NotImplementedError(
            f"OCR back-end {settings.OCR_BACKEND} not implemented"
        )

    api_key = settings.GEMINI_API_KEY
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set in environment")

    # 画像を長辺1024pxに縮小 (無料枠 Flash-Lite の応答速度改善)
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(image_bytes))
        if max(img.size) > 1024:
            ratio = 1024 / max(img.size)
            img = img.resize((int(img.width * ratio), int(img.height * ratio)))
            buf = BytesIO()
            img.convert("RGB").save(buf, format="JPEG", quality=85)
            image_bytes = buf.getvalue()
            mime_type = "image/jpeg"
    except Exception:
        pass

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
            with httpx.Client(timeout=75.0) as cli:
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
            time.sleep(3 * (attempt + 1))
    raise last_exc or RuntimeError("Gemini image API retry exhausted")
