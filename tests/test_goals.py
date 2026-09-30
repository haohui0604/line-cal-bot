"""Phase 3.5: 目的設定ウィザードのテスト."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "tgoal.db")
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.db import init_db, get_conn
from app.services import goals


def setup_module(m):
    init_db()


def _seed_activity(user_id, total_kcal):
    with get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO activity"
            " (user_id, date, total_kcal, source_type) VALUES (?,?,?,?)",
            (user_id, "2026-09-29", total_kcal, "manual"))


def test_weight_wizard_with_activity_data():
    """活動量データがある人は性別・年齢・身長を聞かれない."""
    uid = "UG1"
    _seed_activity(uid, 2400)
    # 体重データも直接投入
    with get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO weight_logs"
            " (user_id, date, weight_kg, is_measured) VALUES (?,?,?,1)",
            (uid, "2026-09-29", 75.0))

    msg = goals.start_wizard(uid)
    assert msg.quick_reply is not None          # 目的はボタン選択
    assert goals.in_wizard(uid)

    r = goals.handle_step(uid, "減量")          # 体重データあり→目標体重へ直行
    assert "目標体重" in r.text
    r = goals.handle_step(uid, "65")
    assert r.quick_reply is not None            # 期間はボタン
    r = goals.handle_step(uid, "期間 60")
    assert "目標確定" not in r.text and "計算結果" in r.text
    assert "2400" in r.text                     # 実績の消費カロリーが使われた
    r = goals.handle_step(uid, "目標確定")
    assert "設定しました" in r.text
    p = goals.get_profile(uid)
    assert p["goal_mode"] == "weight"
    # 赤字 = 10kg*7200/60 = 1200 → 目標 = 2400-1200 = 1200
    assert p["calc_target_kcal"] == 1200
    assert "減量" in goals.context_line(uid)
    assert not goals.in_wizard(uid)             # ウィザード終了


def test_weight_wizard_estimated_from_body():
    """活動量データが無い人は Mifflin-St Jeor で推定."""
    uid = "UG2"
    with get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO weight_logs"
            " (user_id, date, weight_kg, is_measured) VALUES (?,?,?,1)",
            (uid, "2026-09-29", 75.0))

    goals.start_wizard(uid)
    goals.handle_step(uid, "減量")
    goals.handle_step(uid, "65")
    r = goals.handle_step(uid, "期間 60")
    assert "性別" in r.text                     # 推定のため身体情報を聞く
    goals.handle_step(uid, "男性")
    goals.handle_step(uid, "35")
    r = goals.handle_step(uid, "172")
    # BMR = 10*75+6.25*172-5*35+5 = 1655 → *1.4 = 2317 → -1200 = 1117
    assert "1117" in r.text
    goals.handle_step(uid, "目標確定")
    p = goals.get_profile(uid)
    assert p["calc_target_kcal"] == 1117
    assert p["sex"] == "male"


def test_salt_and_muscle_modes():
    uid = "UG3"
    goals.start_wizard(uid)
    r = goals.handle_step(uid, "減塩")
    assert r.quick_reply is not None
    r = goals.handle_step(uid, "塩分OK")
    assert "6.0" in r.text or "6g" in r.text
    assert goals.get_profile(uid)["salt_target_g"] == 6.0
    assert "減塩" in goals.context_line(uid)

    uid2 = "UG4"
    with get_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO weight_logs"
            " (user_id, date, weight_kg, is_measured) VALUES (?,?,?,1)",
            (uid2, "2026-09-29", 75.0))
    goals.start_wizard(uid2)
    r = goals.handle_step(uid2, "筋肉増量")
    assert "120" in r.text                      # 75kg × 1.6 = 120g
    assert goals.get_profile(uid2)["protein_target_g"] == 120.0
    assert "筋肉" in goals.context_line(uid2)


def test_cancel_and_invalid_input():
    uid = "UG5"
    goals.start_wizard(uid)
    goals.handle_step(uid, "減量")
    r = goals.handle_step(uid, "abc")           # 体重に非数値
    assert "数字" in r.text
    r = goals.handle_step(uid, "キャンセル")
    assert "キャンセル" in r.text
    assert not goals.in_wizard(uid)
