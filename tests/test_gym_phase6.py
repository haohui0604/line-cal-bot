"""Phase 6: 質問スレッド／未確認管理／入会申請のワンクリック処理／ジム設定.

- 会員の質問(❓)は comments に author_type='member' で入り、notified_at=NULL が「未確認」
- スタッフが返信 or スレッドを開くと notified_at が入り「確認済み」になる
- 入会申請は画面から承認（担当トレーナー指定）／否認できる
- ジム名の変更・入会コード再発行・QR画像
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "phase6.db"))
os.environ.setdefault("TURSO_DATABASE_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import db, gym_db  # noqa: E402

ADMIN = "Up6admin"
TRAINER = "Up6trainer"
OTHER = "Up6trainer2"
MEMBER = "Up6member"
PENDING = "Up6pending"
GID = 1


@pytest.fixture(scope="module", autouse=True)
def _db():
    settings.DB_PATH = str(Path(tempfile.mkdtemp()) / "gym_phase6.db")
    settings.ADMIN_USER_IDS = ADMIN
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def _seed(_db):
    global GID
    gyms = gym_db.list_admin_gyms(ADMIN)
    GID = int(gyms[0]["gym_id"]) if gyms else int(
        gym_db.create_gym(name="テストジム", owner_user_id=ADMIN)["id"])
    with db.get_conn() as c:
        for uid, nm in ((ADMIN, "管理者"), (TRAINER, "トレーナー"),
                        (OTHER, "別トレーナー"), (MEMBER, "会員"),
                        (PENDING, "申請者")):
            c.execute("INSERT OR REPLACE INTO users (line_user_id, display_name)"
                      " VALUES (?,?)", (uid, nm))
        for uid, role in ((ADMIN, "gym_admin"), (TRAINER, "trainer"),
                          (OTHER, "trainer")):
            c.execute("INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status)"
                      " VALUES (?,?,?,'active')", (GID, uid, role))
        c.execute("INSERT OR IGNORE INTO memberships"
                  " (gym_id,user_id,role,status,trainer_id)"
                  " VALUES (?,?,'member','active',?)", (GID, MEMBER, TRAINER))
        c.execute("UPDATE memberships SET status='active', trainer_id=?"
                  " WHERE gym_id=? AND user_id=?", (TRAINER, GID, MEMBER))
        c.execute("UPDATE memberships SET status='pending', trainer_id=NULL"
                  " WHERE gym_id=? AND user_id=? AND role='member'",
                  (GID, PENDING))
        if not c.execute("SELECT 1 FROM memberships WHERE gym_id=? AND user_id=?"
                         " AND role='member'", (GID, PENDING)).fetchone():
            c.execute("INSERT INTO memberships (gym_id,user_id,role,status)"
                      " VALUES (?,?,'member','pending')", (GID, PENDING))
        c.execute("DELETE FROM comments WHERE user_id=?", (MEMBER,))
    yield


def _ck(uid):
    return {auth.SESSION_COOKIE: auth.issue_session(uid)}


def _member_comment(body="タンパク質が足りませんか？"):
    return gym_db.add_comment(user_id=MEMBER, body=body, author_type="member",
                              author_id=MEMBER, target_date="2026-10-03")


# ---------------- DB 層 ----------------

def test_member_comment_starts_unread():
    cid = _member_comment()
    assert cid > 0
    assert gym_db.count_unread_member_comments(TRAINER) == 1
    assert gym_db.list_unread_member_comments(TRAINER)[0]["body"] == "タンパク質が足りませんか？"


def test_reply_marks_member_comment_read_and_notifies():
    cid = _member_comment("夕食の量は？")
    assert gym_db.count_unread_member_comments(TRAINER) == 1
    rid = gym_db.add_reply(member_id=MEMBER, trainer_id=TRAINER,
                           body="鶏むね100g足しましょう", reply_to_id=cid)
    assert rid > cid
    assert gym_db.count_unread_member_comments(TRAINER) == 0
    thread = gym_db.fetch_member_thread(TRAINER, MEMBER)
    assert thread[-1]["body"] == "鶏むね100g足しましょう"
    assert thread[-1]["author_type"] == "trainer"
    assert thread[-1]["reply_to_id"] == cid
    # 会員の質問は notified_at が入る（確認済み）
    q = [c for c in thread if c["id"] == cid][0]
    assert q["notified_at"]


def test_thread_is_limited_to_assigned_member():
    _member_comment()
    assert gym_db.fetch_member_thread(OTHER, MEMBER) == []
    assert gym_db.mark_thread_read(OTHER, MEMBER) == 0
    assert gym_db.count_unread_member_comments(TRAINER) == 1


def test_mark_thread_read_clears_unread():
    _member_comment("朝の記録が抜けました")
    assert gym_db.count_unread_member_comments(TRAINER) == 1
    assert gym_db.mark_thread_read(TRAINER, MEMBER) == 1
    assert gym_db.count_unread_member_comments(TRAINER) == 0


def test_list_member_threads_shows_unread_first():
    _member_comment()
    rows = gym_db.list_member_threads(TRAINER)
    assert rows and rows[0]["user_id"] == MEMBER
    assert int(rows[0]["unread"]) >= 1


def test_rename_and_regenerate_join_code():
    old = gym_db.get_gym(GID)["join_code"]
    assert gym_db.rename_gym(GID, "リネーム後ジム") is True
    assert gym_db.get_gym(GID)["name"] == "リネーム後ジム"
    new = gym_db.regenerate_join_code(GID)
    assert new and new != old and new.startswith("GYM-")
    assert gym_db.get_gym(GID)["join_code"] == new


# ---------------- HTTP 層 ----------------

def test_questions_page_renders():
    _member_comment()
    r = TestClient(app).get("/trainer/questions", cookies=_ck(TRAINER))
    assert r.status_code == 200
    assert "質問に答える" in r.text
    assert "UI.run" in r.text and "/api/gym/thread/" in r.text


def test_questions_page_requires_staff():
    r = TestClient(app).get("/trainer/questions", cookies=_ck(MEMBER))
    assert r.status_code == 403


def test_thread_api_and_reply():
    cid = _member_comment("質問API テスト")
    c = TestClient(app)
    j = c.get(f"/api/gym/thread/{MEMBER}", cookies=_ck(TRAINER)).json()
    assert any(x["id"] == cid for x in j["comments"])
    assert j["unread"] >= 1
    r = c.post("/api/gym/thread/reply", cookies=_ck(TRAINER),
               json={"member_id": MEMBER, "body": "返信テスト", "reply_to_id": cid})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert gym_db.count_unread_member_comments(TRAINER) == 0


def test_thread_reply_validates():
    c = TestClient(app)
    assert c.post("/api/gym/thread/reply", cookies=_ck(TRAINER),
                  json={"member_id": MEMBER, "body": ""}).status_code == 400
    assert c.post("/api/gym/thread/reply", cookies=_ck(OTHER),
                  json={"member_id": MEMBER, "body": "よそ者"}).status_code == 403


def test_threads_api():
    _member_comment()
    j = TestClient(app).get("/api/gym/threads", cookies=_ck(TRAINER)).json()
    assert "threads" in j and "comments" in j and isinstance(j["unread"], int)


def test_requests_approve_and_reject_api():
    c = TestClient(app)
    with db.get_conn() as conn:
        mid = conn.execute("SELECT id FROM memberships WHERE user_id=? AND"
                           " status='pending'", (PENDING,)).fetchone()["id"]
    r = c.post("/api/gym/requests/approve", cookies=_ck(ADMIN),
               json={"membership_id": mid, "trainer_id": OTHER})
    assert r.status_code == 200 and r.json()["ok"] is True
    with db.get_conn() as conn:
        row = conn.execute("SELECT status, trainer_id FROM memberships WHERE id=?",
                           (mid,)).fetchone()
    assert row["status"] == "active" and row["trainer_id"] == OTHER
    # 二重承認は「処理済み」として弾かれる（404/409 のいずれか）
    assert c.post("/api/gym/requests/approve", cookies=_ck(ADMIN),
                  json={"membership_id": mid}).status_code in (404, 409)


def test_requests_reject_api():
    c = TestClient(app)
    with db.get_conn() as conn:
        conn.execute("UPDATE memberships SET status='pending', trainer_id=NULL"
                     " WHERE gym_id=? AND user_id=? AND role='member'",
                     (GID, PENDING))
        mid = conn.execute("SELECT id FROM memberships WHERE user_id=? AND"
                           " status='pending'", (PENDING,)).fetchone()["id"]
    r = c.post("/api/gym/requests/reject", cookies=_ck(ADMIN),
               json={"membership_id": mid})
    assert r.status_code == 200
    with db.get_conn() as conn:
        assert conn.execute("SELECT status FROM memberships WHERE id=?",
                            (mid,)).fetchone()["status"] == "rejected"


def test_settings_api_requires_gym_admin():
    c = TestClient(app)
    assert c.post("/api/gym/settings", cookies=_ck(TRAINER),
                  json={"gym_id": GID, "name": "だめ"}).status_code == 403
    r = c.post("/api/gym/settings", cookies=_ck(ADMIN),
               json={"gym_id": GID, "name": "設定テストジム"})
    assert r.status_code == 200 and r.json()["name"] == "設定テストジム"
    r2 = c.post("/api/gym/settings", cookies=_ck(ADMIN),
                json={"gym_id": GID, "regenerate_code": True})
    assert r2.status_code == 200 and r2.json()["join_code"].startswith("GYM-")
    assert c.post("/api/gym/settings", cookies=_ck(ADMIN),
                  json={"gym_id": GID}).status_code == 400


def test_qr_endpoint_returns_png():
    r = TestClient(app).get(f"/api/gym/qr?gym_id={GID}", cookies=_ck(ADMIN))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_gym_page_includes_phase6_sections():
    _member_comment()
    r = TestClient(app).get(f"/gym?gym_id={GID}", cookies=_ck(ADMIN))
    assert r.status_code == 200
    for token in ("質問", "/api/gym/qr", "UI.run", "入会コード"):
        assert token in r.text, token
