"""Phase 3.5: 日別ビュー / 記録操作 / トレーナー招待 / AIコメントfallback."""
import os, sys, tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t35.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""  # AI推定無効化（テストを決定的に）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.db import init_db
from app.services import gym_db, day_view


def setup_module(m):
    init_db()


def test_day_view_add_edit():
    gym_db.upsert_user("U35")
    r = day_view.add_entry_manual("U35", date="2026-09-30", meal_slot="lunch",
                                  food_name="牛丼", kcal=700,
                                  protein_g=20, fat_g=25, carb_g=90)
    assert r["ok"] and not r["estimated"]
    d = day_view.build_day_data("U35", "2026-09-30")
    assert d["intake_kcal"] == 700 and "lunch" in d["slots"]
    eid = d["entries"][0]["id"]
    assert day_view.update_entry_full("U35", eid, meal_slot="lunch",
                                      food_name="牛丼(特盛)", kcal=900,
                                      protein_g=25, fat_g=30, carb_g=110)
    d2 = day_view.build_day_data("U35", "2026-09-30")
    assert d2["intake_kcal"] == 900
    assert d2["entries"][0]["food_name"] == "牛丼(特盛)"


def test_pfc_percent():
    out = day_view.pfc_percent_series([(10, 10, 10), (0, 0, 0)])
    assert out["pfc_p"][0] == 23.5 and out["pfc_f"][0] == 52.9
    assert out["pfc_p"][1] == 0  # 記録なし日は0


def test_trainer_invite():
    gym_db.upsert_user("U35O"); gym_db.upsert_user("U35T")
    g = gym_db.create_gym(name="招待ジム", owner_user_id="U35O")
    code = gym_db.create_trainer_invite(g["id"], "U35O")
    assert code.startswith("TR-")
    assert gym_db.use_trainer_invite(code, "U35T")["result"] == "ok"
    assert gym_db.is_staff("U35T")
    assert gym_db.use_trainer_invite(code, "U35O")["result"] in ("used", "already")
    assert gym_db.use_trainer_invite("TR-ZZZZ", "U35O")["result"] == "invalid"
    names = [tr["user_id"] for tr in gym_db.list_trainers(g["id"])]
    assert "U35T" in names


def test_day_comment_rule_fallback():
    from app.services.coach import get_day_comment
    r = get_day_comment("U35", "2026-09-30")  # APIキー無し → rule fallback
    assert "kcal" in r["comment"]
    assert r["source"] == "rule"
