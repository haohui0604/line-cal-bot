# -*- coding: utf-8 -*-
"""Phase 13: 目標の設定日・期限の保存と、残り日数／残りkg／ペースの算出."""
import os, sys, pathlib, tempfile, importlib
os.environ.setdefault("DB_PATH", tempfile.mkdtemp() + "/p13.db")
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services import db as db_mod
from app.services import goals, periods


def setup_module(module):
    db_mod.init_db()


def test_migration_adds_goal_date_columns():
    """init_db で goal_set_at / target_date が冪等に追加される."""
    with db_mod.get_conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(goal_profiles)").fetchall()}
    assert "goal_set_at" in cols
    assert "target_date" in cols


def test_save_profile_stores_set_date_and_deadline():
    goals.save_profile("U_g13", goal_mode="weight", target_weight_kg=70.0,
                       goal_days=90, calc_target_kcal=1800.0,
                       goal_set_at="2026-08-26", target_date="2026-11-24")
    p = goals.get_profile("U_g13")
    assert p["goal_set_at"] == "2026-08-26"
    assert p["target_date"] == "2026-11-24"


def test_goal_schedule_days_left_and_pace():
    db_mod.save_weight(user_id="U_g13", date="2026-10-09", weight_kg=78.0, is_measured=1)
    gs = goals.goal_schedule("U_g13")
    assert gs["set_at"] == "2026-08-26" and gs["set_at_estimated"] is False
    assert gs["target_date"] == "2026-11-24"
    assert gs["days_left"] == goals._days_between(goals.today_jst(), "2026-11-24")
    assert gs["kg_left"] == 8.0
    assert gs["pace_kg_week"] and gs["pace_kg_week"] > 0


def test_goal_schedule_falls_back_to_updated_at():
    """設定日が無い既存データは updated_at で代用し「推定」フラグを立てる."""
    goals.save_profile("U_old13", goal_mode="weight", target_weight_kg=65.0,
                       goal_days=30, calc_target_kcal=1700.0)
    gs = goals.goal_schedule("U_old13")
    assert gs["set_at"] is not None
    assert gs["set_at_estimated"] is True
    assert gs["target_date"] is not None          # set_at + 30日 で補完


def test_goal_schedule_without_goal_is_empty():
    gs = goals.goal_schedule("U_none13")
    assert gs["set_at"] is None and gs["target_date"] is None


def test_summary_exposes_goal_schedule():
    s = periods.build_summary("U_g13", days=14, offset=0)
    for k in ("goal_set_at", "goal_target_date", "goal_days_left",
              "goal_kg_left", "goal_pace_week"):
        assert k in s, k
    assert s["goal_set_at"] == "2026-08-26"
    assert s["goal_target_date"] == "2026-11-24"
    assert s["goal_kg_left"] == 8.0


def test_badge_shows_deadline():
    b = goals.goal_badge("U_g13")
    assert b["label"] == "減量"
    assert "11/24" in b["detail"]


def test_templates_have_goal_info_slot():
    for t in ("app/templates/member_home.html", "app/templates/staff_member_detail.html"):
        html = (ROOT / t).read_text(encoding="utf-8")
        assert 'id="wGoalInfo"' in html, t
    js = (ROOT / "app/static/charts.js").read_text(encoding="utf-8")
    assert "__goal_info__" in js and "goal_pace_week" in js
