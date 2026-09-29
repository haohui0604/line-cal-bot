"""画像 → Gemini OCR → DB保存."""
import base64
import json
import logging
from datetime import date
from linebot.models import TextSendMessage
from app.services.ocr import extract_label
from app.services.db import save_entry

logger = logging.getLogger(__name__)


def handle_image(user_id: str, message_id: str, line_bot_api):
    try:
        content = line_bot_api.get_message_content(message_id)
        image_bytes = b""
        for chunk in content.iter_content():
            image_bytes += chunk
        label = extract_label(image_bytes)
    except Exception as e:
        logger.exception("OCR failed")
        return TextSendMessage(text=f"OCR失敗: {e}\n画像が大きすぎないか確認してください（10MB以下）")

    try:
        data = json.loads(label) if isinstance(label, str) else label
    except json.JSONDecodeError:
        return TextSendMessage(text=f"OCR結果はJSONではない: {label[:200]}")

    food_name = data.get("name") or "未名"
    save_entry(
        user_id=user_id,
        date=date.today().isoformat(),
        meal_slot="snack",  # 食事スロット未確定 → snack枠で一旦記録
        food_name=food_name,
        kcal=float(data.get("kcal") or 0),
        protein_g=data.get("protein_g"),
        fat_g=data.get("fat_g"),
        carb_g=data.get("carb_g"),
        salt_g=data.get("salt_g"),
        quantity_g=data.get("quantity_g"),
        source_type="ocr_label",
        confidence="confirmed",
        linked_image_url=None,
        note=f"brand={data.get('brand')}" if data.get("brand") else None,
    )
    return TextSendMessage(text=_make_reply(data))


def _make_reply(d):
    return (
        f"✅ OCR確定: {d.get('name', '未名')}\n"
        f"   {d.get('kcal', '?')}kcal / "
        f"P{d.get('protein_g', '?')} F{d.get('fat_g', '?')} "
        f"C{d.get('carb_g', '?')} 食塩{d.get('salt_g', '?')}g\n"
        f"   source=ocr_label / confidence=confirmed"
    )
