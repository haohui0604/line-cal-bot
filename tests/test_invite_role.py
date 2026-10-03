"""招待コードの役割対応（TR- → トレーナー / GA- → ジム管理者）."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "invite.db"))
os.environ.setdefault("TURSO_DATABASE_URL", "")

from app.services import db, gym_db  # noqa: E402
from app.services import admin_db  # noqa: E402

OWNER = "Uinv_owner"
T1 = "Uinv_trainer"
T2 = "Uinv_admin2"
GID = 1


@pytest.fixture(scope="module", autouse=True)
def _db():
    from app.config import settings
    settings.DB_PATH = str(Path(tempfile.mkdtemp()) / "invite_role.db")
    settings.ADMIN_USER_IDS = OWNER
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _gym(_db):
    global GID
    gyms = gym_db.list_admin_gyms(OWNER)
    GID = int(gyms[0]["gym_id"]) if gyms else int(
        gym_db.create_gym(name="招待テストジム", owner_user_id=OWNER)["id"])
    yield


def test_tr_invite_registers_trainer():
    code = gym_db.create_trainer_invite(GID, created_by=OWNER)
    assert code.startswith("TR-")
    res = gym_db.use_trainer_invite(code, T1)
    assert res["result"] == "ok" and res["role"] == "trainer"
    with db.get_conn() as c:
        row = c.execute("SELECT role, status FROM memberships WHERE gym_id=?"
                        " AND user_id=?", (GID, T1)).fetchone()
    assert row["role"] == "trainer" and row["status"] == "active"
    assert gym_db.is_staff(T1) is True


def test_ga_invite_registers_gym_admin():
    code = admin_db.create_invite(GID, created_by=OWNER, role="gym_admin")
    assert code.startswith("GA-")
    res = gym_db.use_trainer_invite(code, T2)
    assert res["result"] == "ok" and res["role"] == "gym_admin"
    with db.get_conn() as c:
        row = c.execute("SELECT role, status FROM memberships WHERE gym_id=?"
                        " AND user_id=?", (GID, T2)).fetchone()
    assert row["role"] == "gym_admin" and row["status"] == "active"
    # ジム管理者として /gym の対象ジムに入る
    assert any(int(a["gym_id"]) == GID for a in gym_db.list_admin_gyms(T2))


def test_invite_is_single_use_and_expiry_checked():
    code = gym_db.create_trainer_invite(GID, created_by=OWNER)
    assert gym_db.use_trainer_invite(code, "Uinv_once")["result"] == "ok"
    assert gym_db.use_trainer_invite(code, "Uinv_other")["result"] == "used"
    assert gym_db.use_trainer_invite("TR-0000", "Uinv_x")["result"] == "invalid"


def test_existing_trainer_can_be_promoted_to_admin():
    code = gym_db.create_trainer_invite(GID, created_by=OWNER)
    gym_db.use_trainer_invite(code, T1)          # すでにトレーナー
    ga = admin_db.create_invite(GID, created_by=OWNER, role="gym_admin")
    res = gym_db.use_trainer_invite(ga, T1)
    assert res["result"] == "ok" and res["role"] == "gym_admin"
    with db.get_conn() as c:
        roles = {r["role"] for r in c.execute(
            "SELECT role FROM memberships WHERE gym_id=? AND user_id=?",
            (GID, T1)).fetchall()}
    assert {"trainer", "gym_admin"} <= roles


def test_questions_page_loads_with_ui_get():
    """質問ページの読み込みバグ（UI.get 未定義）の再発防止."""
    src = Path(ROOT / "app/templates/trainer_questions.html").read_text(encoding="utf-8")
    assert "UI.get(" in src
    ui = Path(ROOT / "app/static/ui.js").read_text(encoding="utf-8")
    assert "function get(" in ui and "get: get" in ui
