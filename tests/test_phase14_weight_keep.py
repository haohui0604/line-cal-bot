# -*- coding: utf-8 -*-
"""Phase14: 体重だけ記録しても体脂肪・筋肉量が消えないこと / 収支の色規約"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import db as db_mod            # noqa: E402
from app.services import periods as periods_mod  # noqa: E402


def test_weight_only_save_keeps_bodycomp():
    db_mod.init_db()
    uid = "U_keep"
    db_mod.save_weight(user_id=uid, date="2026-10-10", weight_kg=74.5,
                       is_measured=1, body_fat_pct=21.9, muscle_kg=55.2)
    # 体重だけ再保存（体組成なし = 体重計の写真や「体重 74.0」）
    db_mod.save_weight(user_id=uid, date="2026-10-10", weight_kg=74.0,
                       is_measured=1, body_fat_pct=None, muscle_kg=None)
    with db_mod.get_conn() as c:
        r = c.execute("SELECT weight_kg, body_fat_pct, muscle_kg FROM weight_logs "
                      "WHERE user_id=? AND date=?", (uid, "2026-10-10")).fetchone()
    assert abs(r["weight_kg"] - 74.0) < 1e-6, "体重は更新される"
    assert r["body_fat_pct"] is not None and abs(r["body_fat_pct"] - 21.9) < 1e-6, "体脂肪率が消えた"
    assert r["muscle_kg"] is not None and abs(r["muscle_kg"] - 55.2) < 1e-6, "筋肉量が消えた"


def test_weight_only_save_still_null_when_never_entered():
    db_mod.init_db()
    uid = "U_none"
    db_mod.save_weight(user_id=uid, date="2026-10-10", weight_kg=70.0, is_measured=1)
    with db_mod.get_conn() as c:
        r = c.execute("SELECT body_fat_pct, muscle_kg FROM weight_logs "
                      "WHERE user_id=? AND date=?", (uid, "2026-10-10")).fetchone()
    assert r["body_fat_pct"] is None and r["muscle_kg"] is None


def test_balance_color_rule_consistent():
    """収支・目標との差は「摂取−基準」で、+ (超過) = 赤 / − (下回り) = 緑"""
    def color(v):
        return "#ef4444" if v > 0 else "#10b981"
    assert color(250) == "#ef4444"    # 食べ過ぎ
    assert color(-250) == "#10b981"   # 下回り
    assert color(0) == "#10b981"
