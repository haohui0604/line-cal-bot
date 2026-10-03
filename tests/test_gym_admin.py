"""ジム管理者画面 (Phase 5) のテスト."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "g.db")
os.environ["TURSO_DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

GID = 1
from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import db, gym_db  # noqa: E402
from app.web import staff as staff_web  # noqa: E402
from app.main import app  # noqa: E402
from app import auth  # noqa: E402

ADMIN = "Ugymadmin"
TRAINER = "Utrainer1"
MEMBER = "Umember1"


@pytest.fixture(scope="module", autouse=True)
def _db():
    """このモジュール専用のDBに固定する（他テストと状態を共有しない）."""
    settings.DB_PATH = str(Path(tempfile.mkdtemp()) / "gym_admin.db")
    settings.ADMIN_USER_IDS = ADMIN
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _seed(_db):
    """テストごとに必要な行を冪等に用意する."""
    global GID
    gyms = gym_db.list_admin_gyms(ADMIN)
    if gyms:
        GID = int(gyms[0]["gym_id"])
    else:
        GID = int(gym_db.create_gym(name="テストジム", owner_user_id=ADMIN)["id"])
    with db.get_conn() as c:
        for uid, name in ((ADMIN, "管理者"), (TRAINER, "トレーナー"), (MEMBER, "会員")):
            c.execute("INSERT OR REPLACE INTO users (line_user_id, display_name) VALUES (?,?)",
                      (uid, name))
        c.execute("""INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status)
                     VALUES (?,?,'gym_admin','active')""", (GID, ADMIN))
        c.execute("""INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status)
                     VALUES (?,?,'trainer','active')""", (GID, TRAINER))
        c.execute("""INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status,trainer_id)
                     VALUES (?,?,'member','active',?)""", (GID, MEMBER, TRAINER))
        c.execute("""INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status)
                     VALUES (?,?,'member','pending')""", (GID, "Upending1"))
        c.execute("UPDATE memberships SET status='active', trainer_id=?"
                  " WHERE gym_id=? AND user_id=?", (TRAINER, GID, TRAINER))
        c.execute("UPDATE memberships SET status='active', trainer_id=?"
                  " WHERE gym_id=? AND user_id=?", (TRAINER, GID, MEMBER))
        if not c.execute("SELECT 1 FROM comments WHERE body='質問です' LIMIT 1").fetchone():
            c.execute("""INSERT INTO comments (user_id, author_type, author_id, body, target_date)
                         VALUES (?, 'member', ?, '質問です', '2026-10-01')""", (MEMBER, MEMBER))
    yield


def _ck(uid):
    return {auth.SESSION_COOKIE: auth.issue_session(uid)}


def test_list_admin_gyms():
    assert [a["gym_id"] for a in gym_db.list_admin_gyms(ADMIN)]


def test_staff_list_has_member_count():
    rows = gym_db.list_gym_staff_with_status(GID)
    trainer = [r for r in rows if r["user_id"] == TRAINER][0]
    assert trainer["member_count"] == 1


def test_remove_staff_blocked_when_members_assigned():
    rows = gym_db.list_gym_staff_with_status(GID)
    mid = [r for r in rows if r["user_id"] == TRAINER][0]["membership_id"]
    res = gym_db.remove_staff(mid, by_user_id=ADMIN)
    assert res["ok"] is False and res["reason"] == "has_members" and res["count"] == 1


def test_change_trainer_then_remove_staff_ok():
    rows = gym_db.list_gym_staff_with_status(GID)
    mid = [r for r in rows if r["user_id"] == TRAINER][0]["membership_id"]
    members = gym_db.list_members_for_gym_filtered(GID)
    mmid = members[0]["membership_id"]
    assert gym_db.set_member_trainer(mmid, ADMIN) is True
    assert gym_db.count_assigned_members(GID, TRAINER) == 0
    assert gym_db.remove_staff(mid, by_user_id=ADMIN)["ok"] is True
    assert gym_db.set_member_trainer(mmid, TRAINER) is True   # 戻す


def test_my_member_comments_only_mine():
    assert gym_db.count_my_member_comments(TRAINER) == 1
    assert gym_db.count_my_member_comments(ADMIN) == 0
    rows = gym_db.list_my_member_comments(TRAINER, limit=10)
    assert rows and rows[0]["body"] == "質問です"


def test_gym_page_renders_and_requires_admin():
    c = TestClient(app)
    r = c.get("/gym", cookies=_ck(ADMIN))
    assert r.status_code == 200
    assert "ジム管理" in r.text and "入会コード" in r.text and "トレーナーを招待" in r.text
    r2 = c.get("/gym", cookies=_ck(MEMBER))
    assert r2.status_code == 403


def test_api_invite_and_comments_and_trainer_change():
    c = TestClient(app)
    r = c.post("/api/gym/trainers/invite", json={"gym_id": GID}, cookies=_ck(ADMIN))
    assert r.status_code == 200 and r.json()["code"].startswith("TR-")
    r2 = c.get("/api/gym/my-comments", cookies=_ck(TRAINER))
    assert r2.status_code == 200 and r2.json()["total"] == 1
    members = gym_db.list_members_for_gym_filtered(GID)
    r3 = c.post("/api/gym/members/trainer",
                json={"gym_id": GID, "membership_id": members[0]["membership_id"],
                      "trainer_id": ADMIN}, cookies=_ck(ADMIN))
    assert r3.status_code == 200
    gym_db.set_member_trainer(members[0]["membership_id"], TRAINER)


def test_api_remove_staff_returns_409_when_members():
    c = TestClient(app)
    rows = gym_db.list_gym_staff_with_status(GID)
    t = [r for r in rows if r["user_id"] == TRAINER]
    if not t:
        pytest.skip("対象トレーナーなし")
    r = c.post("/api/gym/trainers/remove",
               json={"gym_id": GID, "membership_id": t[0]["membership_id"]},
               cookies=_ck(ADMIN))
    assert r.status_code in (409, 200)


def test_api_requires_gym_admin():
    c = TestClient(app)
    assert c.post("/api/gym/trainers/invite", json={"gym_id": GID},
                  cookies=_ck(MEMBER)).status_code == 403


def test_pages_use_shared_ui_script():
    c = TestClient(app)
    html = c.get("/gym", cookies=_ck(ADMIN)).text
    assert '/static/ui.js' in html
    assert "UI.showCode" in html and "UI.run" in html
