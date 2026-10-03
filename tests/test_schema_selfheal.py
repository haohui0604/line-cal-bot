"""スキーマのズレ（Turso でカラムが足りない状態）でも画面が500にならないこと.

本番で /trainer と /gym が 500 になった原因は、
PRAGMA が使えない環境で ADDED_COLUMNS の適用が失敗し、
gyms.deleted_at / comments.notified_at 等が欠けたまま
クエリが走っていたこと。
"""
import contextlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DB_PATH", str(Path(tempfile.mkdtemp()) / "selfheal.db"))
os.environ.setdefault("TURSO_DATABASE_URL", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import db, gym_db  # noqa: E402

ADMIN = "Ush_admin"


class _NoDescCursor:
    """PRAGMA の description が取れない環境（libsql 相当）を模す."""

    @property
    def description(self):
        return None

    def fetchall(self):
        return [{}]

    def fetchone(self):
        return {}

    @property
    def rowcount(self):
        return 0


class _BrokenPragmaConn:
    def __init__(self, real):
        self._real = real

    def execute(self, sql, *args):
        if sql.strip().upper().startswith("PRAGMA"):
            return _NoDescCursor()
        return self._real.execute(sql, *args)

    def commit(self):
        return self._real.commit()

    def close(self):
        return self._real.close()

    def sync(self):
        return None


_REAL_GET_CONN = db.get_conn


@contextlib.contextmanager
def _broken_pragma_conn():
    """PRAGMA が壊れた接続で get_conn を差し替えるためのラッパー.

    差し替え後に db.get_conn を呼ぶと再帰するため、実体を退避して使う。
    """
    with _REAL_GET_CONN() as c:
        yield _BrokenPragmaConn(c)


def test_table_columns_uses_select_not_pragma():
    """PRAGMA が壊れていても SELECT からカラム名を取れること."""
    db.init_db()
    with db.get_conn() as c:
        cols = db.table_columns(_BrokenPragmaConn(c), "gyms")
    assert {"id", "name", "join_code"} <= cols


def test_ensure_columns_repairs_schema_when_pragma_broken():
    """PRAGMA が使えなくても不足カラムを追加できること（本番の根本原因）."""
    db.init_db()
    # 本番と同じズレを作る（カラムを落とす）
    with db.get_conn() as c:
        for table, col in (("gyms", "deleted_at"), ("gyms", "deleted_by"),
                           ("comments", "reply_to_id"),
                           ("comments", "notified_at")):
            try:
                c.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        before = db.table_columns(c, "gyms")
    assert "deleted_at" not in before, "前提（カラム欠落）を作れていない"

    original = db.get_conn
    db.get_conn = _broken_pragma_conn
    try:
        db.init_db()
    finally:
        db.get_conn = original

    with db.get_conn() as c:
        gyms = db.table_columns(c, "gyms")
        comments = db.table_columns(c, "comments")
    assert "deleted_at" in gyms and "deleted_by" in gyms
    assert "reply_to_id" in comments and "notified_at" in comments


def test_pages_return_200_after_repair():
    """修復後は /trainer と /gym が 200 になること（500の再発防止）."""
    settings.ADMIN_USER_IDS = ADMIN
    db.init_db()
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO users (line_user_id, display_name)"
                  " VALUES (?,?)", (ADMIN, "オーナー"))
        try:
            c.execute("ALTER TABLE gyms DROP COLUMN deleted_at")
        except sqlite3.OperationalError:
            pass
    gyms = gym_db.list_admin_gyms(ADMIN)
    gid = int(gyms[0]["gym_id"]) if gyms else int(
        gym_db.create_gym(name="自己修復テストジム", owner_user_id=ADMIN)["id"])
    with db.get_conn() as c:
        c.execute("INSERT OR IGNORE INTO memberships (gym_id,user_id,role,status)"
                  " VALUES (?,?,'gym_admin','active')", (gid, ADMIN))
        c.execute("UPDATE memberships SET status='active' WHERE gym_id=? AND"
                  " user_id=?", (gid, ADMIN))

    original = db.get_conn
    db.get_conn = _broken_pragma_conn
    try:
        db.init_db()          # ← 起動時の自己修復
    finally:
        db.get_conn = original

    ck = {auth.SESSION_COOKIE: auth.issue_session(ADMIN)}
    c = TestClient(app)
    r1 = c.get("/trainer", cookies=ck)
    r2 = c.get(f"/gym?gym_id={gid}", cookies=ck)
    r3 = c.get("/trainer/questions", cookies=ck)
    assert r1.status_code == 200, r1.text[:400]
    assert r2.status_code == 200, r2.text[:400]
    assert r3.status_code == 200, r3.text[:400]


def test_list_admin_gyms_without_deleted_at_column():
    """gyms.deleted_at が無い DB でもジム一覧が取れること."""
    db.init_db()
    with db.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO users (line_user_id, display_name)"
                  " VALUES (?,?)", (ADMIN, "オーナー"))
        try:
            c.execute("ALTER TABLE gyms DROP COLUMN deleted_at")
        except sqlite3.OperationalError:
            pass
    rows = gym_db.list_admin_gyms(ADMIN)
    assert isinstance(rows, list)
