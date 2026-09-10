"""画像 → Gemini 解析 (成分表OCR / 食べ物写真推定) → DB保存.

動作:
- mode=label (成分表): confidence=confirmed → 即時DB保存
- mode=photo (料理写真): confidence=estimated → 推定値を提示し、
  「はい」でDB保存 (text_handler の _pending と同一の仕組みを利用)
"""
import json
import logging
import re
from datetime import date

from linebot.models import TextSendMessage

from app.services.ocr import extract_label
from app.services.db import save_entry

logger = logging.getLogger(__name__)


def handle_image(user_id: str, message_id: str, line_bot_api):
    try:
        content = line_bot_api.get_message_content(message_id)
        image_bytes = b"".join(content.iter_content())
        mime = getattr(content, "content_type", None) or "image/jpeg"
        label = extract_label(image_bytes, mime_type=mime)
    except Exception as e:
        logger.exception("image analysis failed")
        return TextSendMessage(text=(
            f"画像の解析に失敗しました ({type(e).__name__})。\n"
            "少し待って再送するか、『朝 食パン100g 250kcal』形式で"
            "直接記録してください"
        ))

    try:
        text = label if isinstance(label, str) else json.dumps(label, ensure_ascii=False)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                      flags=re.MULTILINE)
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        data = json.loads(m.group(0) if m else text)
    except (json.JSONDecodeError, TypeError):
        return TextSendMessage(text="解析結果の読み取りに失敗しました。もう一度撮り直して送ってください")

    mode = data.get("mode") or "photo"

    # 食事に無関係と判断された画像
    if (data.get("kcal") in (0, None)) and data.get("name") in ("不明", None):
        return TextSendMessage(text=(
            "食事の画像として認識できませんでした。\n"
            "料理の写真、または栄養成分表の写真を送ってください"
        ))

    if mode == "label":
        # 成分表: 数値は表記からの厳密な読み取り → 即時保存
        save_entry(
            user_id=user_id,
            date=date.today().isoformat(),
            meal_slot="snack",
            food_name=data.get("name") or data.get("brand") or "未名",
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
        return TextSendMessage(text=_make_label_reply(data))

    # mode == photo: 推定値 → 確認フロー (text_handler と同じ _pending を共有)
    from app.handlers.text_handler import _pending
    foods = [{
        "name": data.get("name") or "不明",
        "kcal": float(data.get("kcal") or 0),
        "protein_g": data.get("protein_g"),
        "fat_g": data.get("fat_g"),
        "carb_g": data.get("carb_g"),
        "salt_g": data.get("salt_g"),
        "quantity_g": data.get("quantity_g"),
    }]
    _pending[user_id] = {"foods": foods, "meal_slot": "snack"}

    reaction = (data.get("reaction") or "").strip()
    lines = []
    if reaction:
        lines += [reaction, ""]
    f = foods[0]
    lines.append("写真からのAI推定 (記録前の確認):")
    lines.append(
        f"・{f['name']} {f['kcal']:.0f}kcal"
        f" (P{f.get('protein_g','?')} F{f.get('fat_g','?')}"
        f" C{f.get('carb_g','?')} 食塩{f.get('salt_g','?')}g)"
    )
    lines += ["", "この内容で記録しますか？ →「はい」/「いいえ」"]
    return TextSendMessage(text="\n".join(lines))


def _make_label_reply(d):
    return (
        f"✅ 成分表から記録: {d.get('name') or d.get('brand') or '未名'}\n"
        f"   {d.get('kcal', '?')}kcal / "
        f"P{d.get('protein_g', '?')} F{d.get('fat_g', '?')} "
        f"C{d.get('carb_g', '?')} 食塩{d.get('salt_g', '?')}g\n"
        f"   source=ocr_label / confidence=confirmed"
    )
