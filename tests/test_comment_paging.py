"""コメントのページング（offset / count）のテスト."""

import sqlite3

from app.services import gym_db


class _CM:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        return self.c

    def __exit__(self, *a):
        return False


def _mem():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, entry_id INTEGER,
        target_date TEXT, author_type TEXT, author_id TEXT, body TEXT,
        is_directive INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    return c


def test_count_and_offset(monkeypatch):
    conn = _mem()
    monkeypatch.setattr(gym_db, "get_conn", lambda: _CM(conn))
    for i in range(5):
        gym_db.add_comment(user_id="u", body="c%d" % i, author_type="trainer")
    assert gym_db.count_comments_for_user("u") == 5
    first = gym_db.fetch_comments_for_user("u", limit=2, offset=0)
    second = gym_db.fetch_comments_for_user("u", limit=2, offset=2)
    third = gym_db.fetch_comments_for_user("u", limit=2, offset=4)
    assert len(first) == 2 and len(second) == 2 and len(third) == 1
    assert len({c["body"] for c in first + second + third}) == 5


def test_offset_beyond_end_is_empty(monkeypatch):
    conn = _mem()
    monkeypatch.setattr(gym_db, "get_conn", lambda: _CM(conn))
    gym_db.add_comment(user_id="u", body="only", author_type="ai_coach")
    assert gym_db.fetch_comments_for_user("u", limit=30, offset=30) == []


def test_other_user_is_isolated(monkeypatch):
    conn = _mem()
    monkeypatch.setattr(gym_db, "get_conn", lambda: _CM(conn))
    gym_db.add_comment(user_id="a", body="a1", author_type="trainer")
    gym_db.add_comment(user_id="b", body="b1", author_type="trainer")
    assert gym_db.count_comments_for_user("a") == 1
    assert [c["body"] for c in gym_db.fetch_comments_for_user("a", limit=30)] == ["a1"]
