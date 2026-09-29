"""テキスト → 食事レコード変換.

入力例:
    '朝 食パン100g 250kcal'
    '昼食 牛丼並盛 700kcal'
    '夜 鶏の水炊き半分 600kcal P40'
"""
import re
from typing import Dict, Optional

# 2文字キーワードを先に評価する (「昼食」で「昼」だけ剥がれて
# 食品名が「食 牛丼」になる事故を防ぐ)
SLOT_KW = {
    "朝食": "breakfast", "昼食": "lunch", "夕食": "dinner",
    "夜食": "snack", "間食": "snack",
    "朝": "breakfast", "昼": "lunch", "夕": "dinner",
    "夜": "dinner", "間": "snack",
}


def parse_record_line(text: str) -> Optional[Dict]:
    """1行テキストをパースしてslot/food_name/kcal/macrosを返す."""
    text = text.strip()
    slot = None
    for k, v in SLOT_KW.items():
        if text == k or text.startswith(k + " ") or (
            len(text) > len(k) and text.startswith(k)
        ):
            slot = v
            text = text[len(k):].strip()
            break
    if slot is None:
        return None

    name = text
    kcal = protein = fat = carb = salt = None

    m_kcal = re.search(r"(\d+(?:\.\d+)?)\s*kcal", text, re.IGNORECASE)
    if m_kcal:
        kcal = float(m_kcal.group(1))
        name = name.replace(m_kcal.group(0), "").strip()

    m_p = re.search(r"(?:^|\s)P\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m_p:
        protein = float(m_p.group(1))
        name = name.replace(m_p.group(0), "").strip()

    m_f = re.search(r"(?:^|\s)F\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m_f:
        fat = float(m_f.group(1))
        name = name.replace(m_f.group(0), "").strip()

    m_c = re.search(r"(?:^|\s)C\s*(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if m_c:
        carb = float(m_c.group(1))
        name = name.replace(m_c.group(0), "").strip()

    m_s = re.search(r"食塩\s*(\d+(?:\.\d+)?)\s*g?", text)
    if m_s:
        salt = float(m_s.group(1))
        name = name.replace(m_s.group(0), "").strip()

    # 末尾のグラム表記を量として吸収
    m_q = re.search(r"(\d+(?:\.\d+)?)\s*g\s*$", name)
    quantity = None
    if m_q:
        quantity = float(m_q.group(1))
        name = name[:m_q.start()].strip()
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        name = "未名"

    return {
        "meal_slot": slot,
        "food_name": name,
        "kcal": kcal or 0.0,
        "protein_g": protein,
        "fat_g": fat,
        "carb_g": carb,
        "salt_g": salt,
        "quantity_g": quantity,
    }
