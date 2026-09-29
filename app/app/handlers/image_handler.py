"""画像 → Gemini 解析 (成分表/料理写真/体重計/消費カロリー) → DB保存.

mode ごとの分岐:
- mode=label    (成分表):   confidence=confirmed → 即時DB保存 (entries)
- mode=photo    (料理写真): confidence=estimated → 確認フロー (_pending 経由)
- mode=weight   (体重計):   confidence=confirmed → 即時DB保存 (weight_logs)
- mode=activity (消費kcal): confidence=confirmed → 即時DB保存 (activity)
"""
import json
import logging
import re
from datetime import date

from linebot.models import TextSendMessage

from app.services.ocr import extract_label
from app.services.db import save_entry, save_weight, save_activity

logger = logging.getLogger(__name__)


def handle_image(user_id: str, message_id: str, line_bot_api):
    try:
        content = line_bot_api.get_message_content(message_id)
        image_bytes = b"".join(content.iter_content())
        mime = getattr(content, "content_type", None) or "image/jpeg"
        raw = extract_label(image_bytes, mime_type=mime)
    except Exception as e:
        logger.exception("image analysis failed")
        return TextSendMessage(text=(
            f"画像の解析に失敗しました ({type(e).__name__})。\n"
            "少し待って再送するか、『朝 食パン100g 250kcal』形式で"
            "直接記録してください"
        ))

    try:
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(),
                      flags=re.MULTILINE)
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        data = json.loads(m.group(0) if m else text)
    except (json.JSONDecodeError, TypeError):
        return TextSendMessage(text="解析結果の読み取りに失敗しました。もう一度撮り直して送ってください")

    mode = data.get("mode") or "photo"

    # ---- 体重計 ----
    if mode == "weight":
        weight_kg = data.get("weight_kg")
        if not weight_kg:
            return TextSendMessage(text=(
                "体重計の数値を読み取れませんでした。\n"
                "撮影した画面にKgの数字がはっきり映っている画像を送ってください"
            ))
        save_weight(
            user_id=user_id, date=date.today().isoformat(),
            weight_kg=float(weight_kg), is_measured=1,
            body_fat_pct=data.get("body_fat_pct"),
            muscle_kg=data.get("muscle_kg"),
            bmr_kcal=data.get("bmr_kcal"),
            note="ocr_image",
        )
        return TextSendMessage(text=_make_weight_reply(data))

    # ---- 消費カロリー (スマートウォッチ/ヘルスケア) ----
    if mode == "activity":
        total = data.get("total_burn_kcal")
        active = data.get("active_kcal")
        resting = data.get("resting_kcal")
        if not total and not active:
            return TextSendMessage(text=(
                "消費カロリーの数値を読み取れませんでした。\n"
                "総消費やアクティブエネルギーのkcalが映っている画面を送ってください"
            ))
        if not total:
            total = (float(active or 0) + float(resting or 0)) or None
        if total is None:
            total = float(active)
        save_activity(
            user_id=user_id, date=date.today().isoformat(),
            total_kcal=float(total),
            active_kcal=float(active) if active else None,
            resting_kcal=float(resting) if resting else None,
            source_type="ocr_image",
            ocr_image_url=None,
        )
        return TextSendMessage(text=_make_activity_reply(data))

    # ---- 無関係画像ガード ----
    if (data.get("kcal") in (0, None)) and data.get("name") in ("不明", None):
        return TextSendMessage(text=(
            "食事/体重計/活動量の画像として認識できませんでした。\n"
            "料理の写真、栄養成分表、体重計、または消費カロリー画面の"
            "スクショを送ってください"
        ))

    # ---- 成分表 ----
    if mode == "label":
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

    # ---- 料理写真 → 推定 → 確認フロー ----
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
    _pending[user_id] = {"foods": foods, "meal_slot": "snack",
                         "date": date.today().isoformat()}

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


def _make_weight_reply(d):
    reaction = (d.get("reaction") or "").strip()
    head = f"⚖️ 体重計から記録: {float(d['weight_kg']):.1f}kg"
    extras = []
    if d.get("body_fat_pct"):
        extras.append(f"体脂肪 {d['body_fat_pct']}%")
    if d.get("muscle_kg"):
        extras.append(f"筋肉 {d['muscle_kg']}kg")
    if d.get("bmr_kcal"):
        extras.append(f"BMR {d['bmr_kcal']}kcal")
    extra_line = (" (" + " / ".join(extras) + ")") if extras else ""
    tail = "\n   source=ocr_image / confidence=confirmed"
    if reaction:
        return f"{reaction}\n{head}{extra_line}{tail}"
    return f"{head}{extra_line}{tail}"


def _make_activity_reply(d):
    reaction = (d.get("reaction") or "").strip()
    head = f"🏃 消費カロリーを記録: 総計 {float(d['total_burn_kcal']):.0f}kcal"
    extras = []
    if d.get("active_kcal"):
        extras.append(f"活動 {d['active_kcal']:.0f}kcal")
    if d.get("resting_kcal"):
        extras.append(f"安静 {d['resting_kcal']:.0f}kcal")
    extra_line = (" (" + " / ".join(extras) + ")") if extras else ""
    tail = ("\n   source=ocr_image / confidence=confirmed\n"
            "『集計』で摂取との収支を確認できます")
    if reaction:
        return f"{reaction}\n{head}{extra_line}{tail}"
    return f"{head}{extra_line}{tail}"
