"""Phase1: migrations/002 goals 適用と目標カロリー関数の検証."""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test.db")

from app.services.db import (  # noqa: E402
    init_db,
    set_goal,
    get_goal,
    fetch_remaining_kcal,
    save_entry,
)


def test_goals_table_and_functions():
    init_db()  # 001 + 002 が両方適用されること

    # 未設定 → None
    assert get_goal("u1", "2026-09-10") is None

    # 同日 upsert
    set_goal(user_id="u1", date="2026-09-10", target_kcal=2000.0)
    assert get_goal("u1", "2026-09-10") == 2000.0
    set_goal(user_id="u1", date="2026-09-10", target_kcal=2100.0)
    assert get_goal("u1", "2026-09-10") == 2100.0

    # 過去日付の目標は「その日以前で最新」を引く
    set_goal(user_id="u1", date="2026-09-01", target_kcal=1800.0)
    assert get_goal("u1", "2026-09-05") == 1800.0
    assert get_goal("u1", "2026-09-10") == 2100.0  # 09-10 が最新

    # 残りカロリー: 目標 2100 - 摂取 500 = 1600
    save_entry(user_id="u1", date="2026-09-10", meal_slot="lunch",
               food_name="麺", kcal=500.0, protein_g=None, fat_g=None,
               carb_g=None, salt_g=None, quantity_g=None,
               source_type="text", confidence="confirmed")
    assert fetch_remaining_kcal("u1", "2026-09-10") == 1600.0
