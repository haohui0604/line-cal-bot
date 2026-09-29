"""Phase 1: ジム入会フロー / users / memberships / comments のテスト.

DB_PATH を一時ファイルに差し替えてから app モジュールを import する。
"""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp()) / "test_gym.db"
os.environ["DB_PATH"] = str(_TMP)
os.environ["TURSO_DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.db import init_db, save_entry, fetch_day_summary
from app.services import gym_db


def setup_module(module):
    init_db()


def test_gym_join_approve_flow():
    gym_db.upsert_user("U_OWNER", "オーナー")
    gym = gym_db.create_gym(name="テストジム", owner_user_id="U_OWNER")
    assert gym["join_code"].startswith("GYM-")

    # 入会申請 → pending
    gym_db.upsert_user("U_MEMBER", "会員A")
    res = gym_db.request_join(user_id="U_MEMBER", gym_id=gym["id"])
    assert res["result"] == "requested"

    # 二重申請は already_pending
    res2 = gym_db.request_join(user_id="U_MEMBER", gym_id=gym["id"])
    assert res2["result"] == "already_pending"

    # 承認 + 担当割当
    assert gym_db.approve_request(res["membership_id"], trainer_id="U_TRAINER")
    ms = gym_db.get_membership_for_user("U_MEMBER")
    assert ms["status"] == "active" and ms["trainer_id"] == "U_TRAINER"

    # 別ジムへの入会は拒否（already_active）
    gym2 = gym_db.create_gym(name="別ジム", owner_user_id="U_OWNER")
    res3 = gym_db.request_join(user_id="U_MEMBER", gym_id=gym2["id"])
    assert res3["result"] == "already_active"

    # トレーナーの担当会員一覧に載る
    members = gym_db.list_members_for_trainer("U_TRAINER")
    assert [m["user_id"] for m in members] == ["U_MEMBER"]


def test_directive_comments():
    cid = gym_db.add_comment(user_id="U_MEMBER", body="タンパク質多めに",
                             author_type="trainer", author_id="U_TRAINER",
                             is_directive=True)
    assert cid > 0
    gym_db.add_comment(user_id="U_MEMBER", body="よく頑張った",
                       author_type="trainer", author_id="U_TRAINER")
    gym_db.add_comment(user_id="U_MEMBER", body="夕食いいですね",
                       author_type="ai_coach")

    directives = gym_db.fetch_active_directives("U_MEMBER")
    assert len(directives) == 1
    assert directives[0]["body"] == "タンパク質多めに"

    all_comments = gym_db.fetch_comments_for_user("U_MEMBER")
    assert len(all_comments) == 3  # trainer directive + trainer + ai_coach


def test_existing_records_unaffected():
    """既存の食事記録ロジックが新規テーブル追加後も動くことを確認."""
    save_entry(user_id="U_MEMBER", date="2026-09-29", meal_slot="lunch",
               food_name="鶏むね", kcal=200, source_type="user_report",
               confidence="estimated")
    s = fetch_day_summary("U_MEMBER", "2026-09-29")
    assert s["intake_kcal"] == 200


def test_session_cookie_roundtrip():
    from app import auth
    token = auth.issue_session("U123")
    assert auth.read_session(token) == "U123"
    assert auth.read_session("tampered-token") is None
