"""日別ビュー用のデータ集約と記録の追加/更新 (Phase 3.5)."""
import logging
from typing import Any, Dict, Optional

from app.services.db import (
    get_conn, save_entry, fetch_entries_for_date, fetch_day_summary,
    fetch_day_activity_total, get_goal, fetch_latest_weight,
)

logger = logging.getLogger(__name__)

SLOT_JP = {
    "breakfast": "朝食", "lunch": "昼食", "dinner": "夕食",
    "snack": "間食", "night": "夜食",
}


def build_day_data(user_id: str, target_date: str) -> Dict[str, Any]:
    """1日分の表示データを集約して返す."""
    entries = fetch_entries_for_date(user_id, target_date)
    s = fetch_day_summary(user_id, target_date)
    goal = get_goal(user_id, target_date)
    burn = fetch_day_activity_total(user_id, target_date)

    weight_kg = None
    try:
        w = fetch_latest_weight(user_id, on_or_before=target_date)
        if w:
            weight_kg = w["weight_kg"] if isinstance(w, dict) else w[1]
    except Exception:
        logger.exception("fetch_latest_weight failed")

    # 区分ごとにグルーピング
    slots: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        slot = e.get("meal_slot") or "snack"
        bucket = slots.setdefault(slot, {"kcal": 0.0, "foods": []})
        bucket["kcal"] += e.get("kcal") or 0
        bucket["foods"].append(e)

    return {
        "date": target_date,
        "slots": slots,
        "entries": entries,
        "intake_kcal": s.get("intake_kcal") or 0,
        "protein_g": s.get("protein_g") or 0,
        "fat_g": s.get("fat_g") or 0,
        "carb_g": s.get("carb_g") or 0,
        "salt_g": s.get("salt_g") or 0,
        "burn_kcal": burn or 0,
        "target_kcal": goal,
        "weight_kg": weight_kg,
    }


def add_entry_manual(user_id: str, *, date: str, meal_slot: str,
                     food_name: str, kcal: float,
                     protein_g=None, fat_g=None, carb_g=None, salt_g=None,
                     source: str = "user_report") -> Dict[str, Any]:
    """手動追加。PFCが未指定ならAI推定で補完を試みる."""
    confidence = "confirmed"
    estimated = False
    if (kcal is None or protein_g is None or fat_g is None
            or carb_g is None):
        try:
            from app.services.llm import estimate_food_single
            item = {"food_name": food_name, "quantity_g": None}
            estimate_food_single(item)  # item を in-place 更新
            kcal = kcal if kcal is not None else item.get("kcal")
            protein_g = protein_g if protein_g is not None else item.get("protein_g")
            fat_g = fat_g if fat_g is not None else item.get("fat_g")
            carb_g = carb_g if carb_g is not None else item.get("carb_g")
            salt_g = salt_g if salt_g is not None else item.get("salt_g")
            confidence = "estimated"
            estimated = True
        except Exception:
            logger.exception("kcal/PFC estimation failed")
    if kcal is None:
        raise ValueError(
            "カロリーを推定できませんでした。kcal を入力してください")
    save_entry(
        user_id=user_id, date=date, meal_slot=meal_slot, food_name=food_name,
        kcal=kcal, protein_g=protein_g, fat_g=fat_g, carb_g=carb_g,
        salt_g=salt_g, source_type=source, confidence=confidence,
    )
    return {"ok": True, "estimated": estimated}


def update_entry_full(user_id: str, entry_id: int, *, meal_slot: str,
                      food_name: str, kcal: float,
                      protein_g=None, fat_g=None, carb_g=None,
                      salt_g=None) -> bool:
    """既存記録の全項目更新."""
    with get_conn() as c:
        cur = c.execute(
            """UPDATE entries SET meal_slot=?, food_name=?, kcal=?,
                 protein_g=?, fat_g=?, carb_g=?, salt_g=?,
                 updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (meal_slot, food_name, kcal, protein_g, fat_g, carb_g, salt_g,
             entry_id, user_id))
        return cur.rowcount > 0


def pfc_percent_series(summaries) -> Dict[str, list]:
    """14日分の日次サマリー列から PFC のkcal比率(%)系列を作る."""
    ps, fs, cs = [], [], []
    for p, f, c in summaries:
        pk, fk, ck = (p or 0) * 4, (f or 0) * 9, (c or 0) * 4
        total = pk + fk + ck
        if total > 0:
            ps.append(round(pk / total * 100, 1))
            fs.append(round(fk / total * 100, 1))
            cs.append(round(ck / total * 100, 1))
        else:
            ps.append(0); fs.append(0); cs.append(0)
    return {"pfc_p": ps, "pfc_f": fs, "pfc_c": cs}
