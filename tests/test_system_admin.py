"""システム管理者画面 (Phase 4) のテスト.

DB_PATH を一時ファイルへ向け、migration 010 が冪等に効くこと、
権限判定・ジム削除時の担当解除・監査ログ・KPI を検証する。
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp()) / "t.db")
os.environ["TURSO_DATABASE_URL"] = ""
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app.config import settings  # noqa: E402
from app.services import admin_db, db  # noqa: E402
from app.web import admin as admin_web  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _init():
    db.init_db()


def _req(cookie_uid=None):
    headers = []
    if cookie_uid:
        from app import auth
        headers = [(b"cookie", f"{auth.SESSION_COOKIE}={auth.issue_session(cookie_uid)}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/system",
                    "headers": headers, "query_string": b""})


def test_migration_creates_tables():
    with db.get_conn() as c:
        names = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "system_admins" in names and "audit_logs" in names


def test_init_db_is_idempotent():
    db.init_db(); db.init_db()   # 2回目以降も落ちない
    with db.get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM system_admins").fetchone()[0] >= 0


def test_added_columns_applied():
    with db.get_conn() as c:
        def cols(t):
            return {r[1] for r in c.execute(f"PRAGMA table_info({t})").fetchall()}
        assert {"deleted_at", "deleted_by"} <= cols("gyms")
        assert {"removed_by", "removed_at", "removed_reason"} <= cols("memberships")
        assert {"reply_to_id", "notified_at"} <= cols("comments")
        assert "role" in cols("trainer_invites")


def test_env_admin_is_system_admin():
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = "Uenv1"
    try:
        assert admin_db.is_system_admin("Uenv1") is True
        assert admin_db.is_system_admin("Uother") is False
        assert admin_db.is_system_admin("") is False
    finally:
        settings.ADMIN_USER_IDS = old


def test_bootstrap_env_admins_idempotent():
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = "Uboot1,Uboot2"
    try:
        admin_db.bootstrap_env_admins()
        admin_db.bootstrap_env_admins()
        ids = {a["user_id"] for a in admin_db.list_system_admins()}
        assert {"Uboot1", "Uboot2"} <= ids
    finally:
        settings.ADMIN_USER_IDS = old


def test_last_system_admin_cannot_be_removed():
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = ""
    try:
        with db.get_conn() as c:
            c.execute("DELETE FROM system_admins")
        admin_db.add_system_admin("Uonly")
        assert admin_db.remove_system_admin("Uonly") is False   # 最後の1人
        admin_db.add_system_admin("Usecond")
        assert admin_db.remove_system_admin("Uonly") is True
    finally:
        settings.ADMIN_USER_IDS = old


def test_create_and_soft_delete_gym_clears_trainer():
    g = admin_db.create_gym("テストジム")
    assert g["join_code"].startswith("GYM-")
    with db.get_conn() as c:
        c.execute("INSERT INTO users (line_user_id, display_name) VALUES ('Um','会員')")
        c.execute("""INSERT INTO memberships (gym_id, user_id, role, status, trainer_id)
                     VALUES (?, 'Um', 'member', 'active', 'Utr')""", (g["id"],))
    before = [x for x in admin_db.list_gyms() if x["id"] == g["id"]][0]
    assert before["members"] == 1

    admin_db.soft_delete_gym(g["id"], "Uroot", "test")
    after = [x for x in admin_db.list_gyms() if x["id"] == g["id"]][0]
    assert after["deleted_at"]
    with db.get_conn() as c:
        row = c.execute("SELECT status, trainer_id, removed_reason FROM memberships "
                        "WHERE gym_id=? AND user_id='Um'", (g["id"],)).fetchone()
    assert row[0] == "left" and row[2] == "test"
    # 担当IDは「復元できるように」保持し、有効な担当としては扱わない
    assert row[1] == "Utr"
    with db.get_conn() as c:
        active = c.execute("SELECT COUNT(*) AS n FROM memberships "
                           "WHERE gym_id=? AND status='active'", (g["id"],)).fetchone()["n"]
    assert active == 0


def test_invite_role_prefix():
    g = admin_db.create_gym("招待テスト")
    assert admin_db.create_invite(g["id"], "Uroot", role="gym_admin").startswith("GA-")
    assert admin_db.create_invite(g["id"], "Uroot", role="trainer").startswith("TR-")


def test_audit_log_records_and_lists():
    admin_db.add_audit("Uroot", "gym.create", "gym", "1", "メモ")
    rows = admin_db.list_audit(5)
    assert rows and rows[0]["action"] == "gym.create" and rows[0]["actor_user_id"] == "Uroot"


def test_analytics_shape():
    a = admin_db.analytics()
    for k in ("gyms_total", "gyms_active", "users_total", "members_active",
              "trainers_active", "admins_active", "pending_requests"):
        assert k in a
    assert isinstance(a["gyms_needing_attention"], list)


def test_require_system_admin_401_and_403():
    with pytest.raises(HTTPException) as e1:
        admin_web._require_system_admin(_req(None))
    assert e1.value.status_code == 401
    with pytest.raises(HTTPException) as e2:
        admin_web._require_system_admin(_req("Unobody"))
    assert e2.value.status_code == 403


def test_require_system_admin_ok():
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = "Uok"
    try:
        assert admin_web._require_system_admin(_req("Uok")) == "Uok"
    finally:
        settings.ADMIN_USER_IDS = old


def test_system_page_redirects_when_not_logged_in():
    resp = admin_web.system_home(_req(None))
    assert getattr(resp, "status_code", 200) in (302, 307)


def test_system_page_renders_for_admin():
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = "Uok"
    try:
        resp = admin_web.system_home(_req("Uok"))
        html = resp.body.decode("utf-8")
        assert "システム管理" in html and "運営KPI" in html
    finally:
        settings.ADMIN_USER_IDS = old


def test_api_analytics_requires_admin():
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = "Uok"
    try:
        assert "gyms_total" in admin_web.api_analytics(_req("Uok"))
    finally:
        settings.ADMIN_USER_IDS = old


def test_gym_create_requires_name():
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = "Uok"
    try:
        with pytest.raises(HTTPException) as e:
            admin_web.api_gym_create(_req("Uok"), {"name": "  "})
        assert e.value.status_code == 400
    finally:
        settings.ADMIN_USER_IDS = old


def _script_of(html: str) -> str:
    import re
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))


def _admin_html() -> str:
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = "Uok"
    try:
        return admin_web.system_home(_req("Uok")).body.decode("utf-8")
    finally:
        settings.ADMIN_USER_IDS = old


def test_page_script_has_no_escaped_quote_bug():
    """JS を壊す \\" が無いこと（全ボタン無反応の再発防止）."""
    script = _script_of(_admin_html())
    assert '\\"' not in script
    for fn in ("post", "createGym", "delGym", "inviteAdmin", "loadStaff",
               "rmMember", "addAdmin", "rmAdmin"):
        assert ("function " + fn) in script, fn


def test_page_script_parses_with_node_if_available():
    """実描画した script が構文エラーを含まないこと."""
    import shutil, subprocess
    node = shutil.which("node")
    if not node:
        pytest.skip("node が無い環境ではスキップ")
    p = Path(tempfile.mkdtemp()) / "s.js"
    p.write_text(_script_of(_admin_html()), encoding="utf-8")
    r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_create_gym_when_table_missing_self_heals(tmp_path, monkeypatch):
    """gyms が無い環境でも 500 にならず作成できること（隔離DBで検証）."""
    monkeypatch.setattr(db.settings, "DB_PATH", str(tmp_path / "iso.db"))
    db.init_db()
    with db.get_conn() as c:
        c.execute("DROP TABLE IF EXISTS gyms")
    g = admin_db.create_gym("自己修復テスト")      # 例外にならない
    assert g["id"] and g["join_code"].startswith("GYM-")
    with db.get_conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM gyms").fetchone()["n"] >= 1
    monkeypatch.undo()
    db.init_db()


def test_restore_gym_reactivates_members():
    db.init_db()
    from app.services.db import get_conn
    g = admin_db.create_gym("復元テスト")
    with get_conn() as c:
        c.execute("INSERT INTO users (line_user_id, display_name) VALUES ('Ur','会員')")
        c.execute("""INSERT INTO memberships (gym_id, user_id, role, status, trainer_id)
                     VALUES (?, 'Ur', 'member', 'active', 'Utr')""", (g["id"],))
    admin_db.soft_delete_gym(g["id"], "Uroot", "test")
    with get_conn() as c:
        row = c.execute("SELECT status, trainer_id FROM memberships WHERE gym_id=?",
                        (g["id"],)).fetchone()
    assert row["status"] == "left" and row["trainer_id"] == "Utr"   # 担当は消さない
    assert admin_db.restore_gym(g["id"], "Uroot") is True
    with get_conn() as c:
        row = c.execute("SELECT status, trainer_id FROM memberships WHERE gym_id=?",
                        (g["id"],)).fetchone()
    assert row["status"] == "active" and row["trainer_id"] == "Utr"


def test_count_active_gyms():
    db.init_db()
    n = admin_db.count_active_gyms()
    assert isinstance(n, int) and n >= 0


def test_e2e_create_delete_recreate_returns_200():
    """作成→招待→削除→再作成→復元 がすべて 200（500の再発防止）."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app import auth as _auth
    old = settings.ADMIN_USER_IDS
    settings.ADMIN_USER_IDS = "Ue2e"
    try:
        db.init_db()
        c = TestClient(app)
        ck = {_auth.SESSION_COOKIE: _auth.issue_session("Ue2e")}
        r1 = c.post("/api/system/gym/create", json={"name": "E2E-A"}, cookies=ck)
        assert r1.status_code == 200, r1.text
        gid = r1.json()["id"]
        assert c.post("/api/system/gym/admin/invite", json={"gym_id": gid},
                      cookies=ck).status_code == 200
        assert c.post("/api/system/gym/delete", json={"gym_id": gid},
                      cookies=ck).status_code == 200
        r2 = c.post("/api/system/gym/create", json={"name": "E2E-B"}, cookies=ck)
        assert r2.status_code == 200, r2.text
        assert c.post("/api/system/gym/restore", json={"gym_id": gid},
                      cookies=ck).status_code == 200
    finally:
        settings.ADMIN_USER_IDS = old
