"""SQLite 永続化."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any
import logging

logger = logging.getLogger(__name__)


@contextmanager
def get_conn():
    p = Path(__import__("app.config", fromlist=["settings"]).settings.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    schema = Path(__file__).resolve().parents[2] / "migrations" / "001_init.sql"
    sql = schema.read_text(encoding="utf-8")
    with get_conn() as c:
        c.executescript(sql)
    logger.info("DB schema initialized")


def save_entry(*, user_id: str, date: str, meal_slot: str, food_name: str,
               kcal: float, protein_g=None, fat_g=None, carb_g=None,
               salt_g=None, quantity_g=None, source_type: str,
               confidence: str, linked_image_url=None, note=None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO entries
            (user_id, date, meal_slot, food_name, kcal, protein_g, fat_g,
             carb_g, salt_g, quantity_g, source_type, confidence,
             linked_image_url, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, date, meal_slot, food_name) DO UPDATE SET
              kcal=excluded.kcal, protein_g=excluded.protein_g,
              fat_g=excluded.fat_g, carb_g=excluded.carb_g,
              salt_g=excluded.salt_g, quantity_g=excluded.quantity_g,
              source_type=excluded.source_type, confidence=excluded.confidence,
              updated_at=CURRENT_TIMESTAMP
        """, (user_id, date, meal_slot, food_name, kcal, protein_g,
              fat_g, carb_g, salt_g, quantity_g, source_type, confidence,
              linked_image_url, note))


def save_activity(*, user_id: str, date: str, total_kcal: float,
                  active_kcal=None, resting_kcal=None, source_type: str,
                  ocr_image_url=None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO activity
            (user_id, date, total_kcal, active_kcal, resting_kcal,
             source_type, ocr_image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
              total_kcal=excluded.total_kcal, active_kcal=excluded.active_kcal,
              resting_kcal=excluded.resting_kcal, source_type=excluded.source_type
        """, (user_id, date, total_kcal, active_kcal, resting_kcal,
              source_type, ocr_image_url))


def save_weight(*, user_id: str, date: str, weight_kg: float, is_measured: int,
                body_fat_pct=None, muscle_kg=None, bmr_kcal=None, note=None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO weight_logs
            (user_id, date, weight_kg, is_measured, body_fat_pct, muscle_kg,
             bmr_kcal, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
              weight_kg=excluded.weight_kg, is_measured=excluded.is_measured,
              body_fat_pct=excluded.body_fat_pct, muscle_kg=excluded.muscle_kg,
              bmr_kcal=excluded.bmr_kcal
        """, (user_id, date, weight_kg, is_measured, body_fat_pct,
              muscle_kg, bmr_kcal, note))


def fetch_day_summary(user_id: str, date: str) -> Dict[str, Any]:
    with get_conn() as c:
        r = c.execute("""
            SELECT COALESCE(SUM(kcal),0) AS intake_kcal,
                   COALESCE(SUM(protein_g),0) AS protein_g,
                   COALESCE(SUM(fat_g),0) AS fat_g,
                   COALESCE(SUM(carb_g),0) AS carb_g,
                   COALESCE(SUM(salt_g),0) AS salt_g
            FROM entries WHERE user_id=? AND date=?
        """, (user_id, date)).fetchone()
        a = c.execute("""
            SELECT total_kcal FROM activity
            WHERE user_id=? AND date=?
        """, (user_id, date)).fetchone()
    intake = r["intake_kcal"]
    burn = a["total_kcal"] if a else 0
    return {
        "date": date,
        "intake_kcal": intake,
        "burn_kcal": burn,
        "deficit_kcal": burn - intake,
        "protein_g": r["protein_g"],
        "fat_g": r["fat_g"],
        "carb_g": r["carb_g"],
        "salt_g": r["salt_g"],
    }


def fetch_recent_history(user_id: str, days: int = 7) -> List[Dict[str, Any]]:
    with get_conn() as c:
        rows = c.execute(
            """
            SELECT d.date,
                   COALESCE((SELECT SUM(kcal) FROM entries
                              WHERE user_id=:uid AND date=d.date), 0) AS intake_kcal,
                   COALESCE((SELECT total_kcal FROM activity
                              WHERE user_id=:uid AND date=d.date), 0) AS consumed_kcal
            FROM (
                SELECT date FROM entries  WHERE user_id=:uid
                UNION
                SELECT date FROM activity WHERE user_id=:uid
            ) d
            ORDER BY d.date DESC
            LIMIT :days
            """,
            {"uid": user_id, "days": days},
        ).fetchall()
    return [
        {"date": r["date"],
         "intake_kcal": r["intake_kcal"],
         "consumed_kcal": r["consumed_kcal"],
         "deficit_kcal": r["consumed_kcal"] - r["intake_kcal"]}
        for r in rows
    ]

