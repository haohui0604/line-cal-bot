"""Phase 8: 会員の既読/新着・⭐指導方針の運用・同一ジム共有（同意ベース）."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "phase8.db"))
os.environ.setdefault("TURSO_DATABASE_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import db, gym_db  # noqa: E402
import app.web.member as member_web  # noqa: E402

ADMIN = "Up8_admin"
TRAINER = "Up8_trainer"
OTHER = "Up8_trainer2"
MEMBER = "Up8_member"
GID = 1


@pytest.fixture(scope="module", autouse=True)
def _db():
    settings.DB_PATH = str(Path(tempfile.mkdtemp()) / "gym_phase8.db")
    settings.ADMIN_USER_IDS = ADMIN
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _seed(_db):
    global GID
    gyms = gym_db.list_admin_gyms(ADMIN)
    GID = int(gyms[0]["gym_id"]) if gyms else int(
        gym_db.create_gym(name="Phase8ジム", owner_user_id=ADMIN)["id"])
    with db.get_conn() as c:
        for uid, nm in ((ADMIN, "管理者"), (TRAINER, "担当トレーナー"),
                        (OTHER, "別トレーナー"), (MEMBER, "会員")):
            c.execute("INSERT OR REPLACE INTO users (line_user_id, display_name)"
                      " VALUES (?,?)", (uid, nm))
        for uid, role in ((ADMIN, "gym_admin"), (TRAINER, "trainer"), (OTHER, "trainer")):
            c.execute("INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status)"
                      " VALUES (?,?,?,'active')", (GID, uid, role))
        c.execute("INSERT OR IGNORE INTO memberships"
                  " (gym_id,user_id,role,status,trainer_id)"
                  " VALUES (?,?,'member','active',?)", (GID, MEMBER, TRAINER))
        c.execute("UPDATE memberships SET status='active', trainer_id=?,"
                  " data_share_scope='assigned', consent_at=NULL"
                  " WHERE gym_id=? AND user_id=? AND role='member'",
                  (TRAINER, GID, MEMBER))
        c.execute("DELETE FROM comments WHERE user_id=?", (MEMBER,))
    yield


def _ck(uid):
    return {auth.SESSION_COOKIE: auth.issue_session(uid)}


@pytest.fixture
def member_client(monkeypatch):
    monkeypatch.setattr(member_web, "_verify_uid", lambda tok: MEMBER)
    return TestClient(app)


# ---------------- 1) 会員の既読/新着 ----------------

def test_member_sees_new_and_mark_read():
    cid = gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="夜は軽めに")
    assert gym_db.count_unread_comments_for_member(MEMBER) == 1
    items = gym_db.fetch_comments_for_user(MEMBER, limit=5)
    assert items[0]["is_new"] is True and items[0]["id"] == cid
    assert gym_db.mark_member_comments_read(MEMBER) == 1
    assert gym_db.count_unread_comments_for_member(MEMBER) == 0
    assert gym_db.fetch_comments_for_user(MEMBER, limit=5)[0]["is_new"] is False


def test_own_comments_are_not_marked_new():
    gym_db.add_comment(user_id=MEMBER, body="自分のメモ", author_type="member",
                       author_id=MEMBER)
    gym_db.mark_member_comments_read(MEMBER)
    assert gym_db.count_unread_comments_for_member(MEMBER) == 0


def test_comments_api_exposes_unread(member_client):
    gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="新着テスト")
    j = member_client.post("/api/me/comments", json={"id_token": "dummy"}).json()
    assert j["unread"] >= 1
    assert j["comments"][0]["is_new"] is True
    r = member_client.post("/api/me/comments/read", json={"id_token": "dummy"}).json()
    assert r["ok"] is True and r["unread"] == 0


# ---------------- 2) ⭐指導方針の運用 ----------------

def test_directive_is_fed_to_ai_with_date_and_dedupe():
    gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="タンパク質を毎食20g",
                     is_directive=True)
    gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="タンパク質を毎食20g",
                     is_directive=True)
    gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="方針ではない一言")
    ds = gym_db.fetch_active_directives(MEMBER)
    assert len(ds) == 1                      # 同文は畳まれる
    assert ds[0]["body"] == "タンパク質を毎食20g"
    assert ds[0]["created_at"]


def test_directive_can_be_listed_and_revoked():
    cid = gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="有酸素は週3",
                           is_directive=True)
    assert any(d["id"] == cid for d in gym_db.list_active_directives(MEMBER))
    res = gym_db.set_directive(cid, False, TRAINER)
    assert res["ok"] is True and res["is_directive"] is False
    assert not any(d["id"] == cid for d in gym_db.list_active_directives(MEMBER))


def test_directive_toggle_requires_view_permission():
    cid = gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="権限テスト",
                           is_directive=True)
    outsider = "Up8_outsider"
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO users (line_user_id, display_name)"
                  " VALUES (?,?)", (outsider, "部外者"))
    assert gym_db.set_directive(cid, False, outsider) == {"error": "forbidden"}


def test_directive_api_endpoints():
    cid = gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="API方針",
                           is_directive=True)
    c = TestClient(app)
    j = c.get(f"/api/gym/directives/{MEMBER}", cookies=_ck(TRAINER)).json()
    assert any(d["id"] == cid for d in j["directives"])
    r = c.post("/api/gym/directive/toggle", cookies=_ck(TRAINER),
               json={"comment_id": cid, "on": False})
    assert r.status_code == 200 and r.json()["is_directive"] is False
    assert c.post("/api/gym/directive/toggle", cookies=_ck(TRAINER),
                  json={}).status_code == 400


# ---------------- 3) 同一ジム共有（同意ベース） ----------------

def test_default_scope_is_assigned_only():
    assert gym_db.can_staff_view_member(TRAINER, MEMBER) is True
    assert gym_db.can_staff_view_member(OTHER, MEMBER) is False
    assert all(m["user_id"] != MEMBER
               for m in gym_db.list_members_for_staff(OTHER))


def test_consent_opens_gym_wide_visibility():
    gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="共有スレッド確認")
    res = gym_db.set_member_share_scope(MEMBER, "gym")
    assert res["ok"] is True and res["scope"] == "gym"
    scope = gym_db.get_member_share_scope(MEMBER)
    assert scope["share_scope"] == "gym" and scope["consent_at"]
    assert gym_db.can_staff_view_member(OTHER, MEMBER) is True
    assert any(m["user_id"] == MEMBER for m in gym_db.list_members_for_staff(OTHER))
    assert gym_db.fetch_member_thread(OTHER, MEMBER) != []


def test_scope_can_be_reverted():
    gym_db.set_member_share_scope(MEMBER, "gym")
    assert gym_db.set_member_share_scope(MEMBER, "assigned")["scope"] == "assigned"
    assert gym_db.can_staff_view_member(OTHER, MEMBER) is False
    assert gym_db.get_member_share_scope(MEMBER)["consent_at"] is None


def test_share_scope_api(member_client):
    j = member_client.post("/api/me/share-scope", json={"id_token": "dummy"}).json()
    assert "share" in j
    r = member_client.post("/api/me/share-scope",
                           json={"id_token": "dummy", "scope": "gym"}).json()
    assert r["ok"] is True and r["share"]["share_scope"] == "gym"
    r2 = member_client.post("/api/me/share-scope",
                            json={"id_token": "dummy", "scope": "assigned"}).json()
    assert r2["share"]["share_scope"] == "assigned"


def test_pages_render_phase8_ui():
    gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER, body="UIテスト",
                     is_directive=True)
    c = TestClient(app)
    detail = c.get(f"/trainer/members/{MEMBER}", cookies=_ck(TRAINER))
    assert detail.status_code == 200
    assert "/api/gym/directives/" in detail.text and "指導方針" in detail.text
    q = c.get("/trainer/questions", cookies=_ck(TRAINER))
    assert q.status_code == 200
