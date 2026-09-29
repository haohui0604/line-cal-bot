"""ジム / ユーザー / 担当紐づけ / コメントの永続化 (006_gym.sql).

既存の db.py の get_conn() をそのまま利用し、SQLite/Turso 両対応のまま動く。
"""
import secrets
from typing import Optional, List, Dict, Any

from app.services.db import get_conn


def _d(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# ---- users ----

def upsert_user(line_user_id: str, display_name: Optional[str] = None,
                picture_url: Optional[str] = None) -> None:
    """ユーザーを upsert。名前・画像は None なら既存値を維持."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO users (line_user_id, display_name, picture_url)
            VALUES (?, ?, ?)
            ON CONFLICT(line_user_id) DO UPDATE SET
              display_name = COALESCE(excluded.display_name, users.display_name),
              picture_url  = COALESCE(excluded.picture_url,  users.picture_url),
              updated_at   = CURRENT_TIMESTAMP
        """, (line_user_id, display_name, picture_url))


def get_user(line_user_id: str) -> Optional[Dict[str, Any]]:
    with get_conn() as c:
        return _d(c.execute(
            "SELECT * FROM users WHERE line_user_id=?", (line_user_id,)
        ).fetchone())


# ---- gyms ----

def _unique_join_code(c) -> str:
    for _ in range(20):
        code = "GYM-" + secrets.token_hex(2).upper()  # 例: GYM-8F3A
        hit = c.execute("SELECT 1 FROM gyms WHERE join_code=?", (code,)).fetchone()
        if not hit:
            return code
    raise RuntimeError("join_code の採番に失敗しました")


def create_gym(*, name: str, owner_user_id: str) -> Dict[str, Any]:
    """ジムを作成し、オーナーを gym_admin として active 登録する."""
    with get_conn() as c:
        code = _unique_join_code(c)
        cur = c.execute(
            "INSERT INTO gyms (name, join_code, owner_user_id) VALUES (?,?,?)",
            (name, code, owner_user_id))
        gym_id = cur.lastrowid
        c.execute(
            "INSERT INTO memberships (gym_id, user_id, role, status)"
            " VALUES (?,?, 'gym_admin', 'active')",
            (gym_id, owner_user_id))
    return {"id": gym_id, "name": name, "join_code": code,
            "owner_user_id": owner_user_id}


def find_gym_by_code(code: str) -> Optional[Dict[str, Any]]:
    with get_conn() as c:
        return _d(c.execute(
            "SELECT * FROM gyms WHERE UPPER(join_code)=UPPER(?)", (code,)
        ).fetchone())


# ---- memberships ----

def request_join(*, user_id: str, gym_id: int) -> Dict[str, Any]:
    """会員の入会申請。結果を {"result": ...} で返す."""
    with get_conn() as c:
        # 別ジムを含む active 会員登録があれば二重登録を防ぐ
        active = c.execute(
            "SELECT m.id, g.name AS gym_name FROM memberships m"
            " JOIN gyms g ON g.id=m.gym_id"
            " WHERE m.user_id=? AND m.role='member' AND m.status='active'",
            (user_id,)).fetchone()
        if active:
            return {"result": "already_active", "gym_name": active["gym_name"]}

        existing = c.execute(
            "SELECT id, status FROM memberships"
            " WHERE gym_id=? AND user_id=? AND role='member'",
            (gym_id, user_id)).fetchone()
        if existing:
            if existing["status"] == "pending":
                return {"result": "already_pending",
                        "membership_id": existing["id"]}
            if existing["status"] in ("rejected", "left"):
                c.execute(
                    "UPDATE memberships SET status='pending',"
                    " updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (existing["id"],))
                return {"result": "requested",
                        "membership_id": existing["id"]}
            return {"result": "already_active",
                    "membership_id": existing["id"]}

        cur = c.execute(
            "INSERT INTO memberships (gym_id, user_id, role, status)"
            " VALUES (?,?, 'member', 'pending')", (gym_id, user_id))
        return {"result": "requested", "membership_id": cur.lastrowid}


def approve_request(membership_id: int,
                    trainer_id: Optional[str] = None) -> bool:
    """入会申請を承認し、担当トレーナーを割り当てる."""
    with get_conn() as c:
        cur = c.execute(
            "UPDATE memberships SET status='active',"
            " trainer_id=COALESCE(?, trainer_id), updated_at=CURRENT_TIMESTAMP"
            " WHERE id=? AND status='pending'", (trainer_id, membership_id))
        return cur.rowcount > 0


def reject_request(membership_id: int) -> bool:
    with get_conn() as c:
        cur = c.execute(
            "UPDATE memberships SET status='rejected',"
            " updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            (membership_id,))
        return cur.rowcount > 0


def get_membership_for_user(user_id: str) -> Optional[Dict[str, Any]]:
    """そのユーザーの最新の membership（ジム名つき）."""
    with get_conn() as c:
        return _d(c.execute(
            "SELECT m.id, m.gym_id, m.role, m.status, m.trainer_id,"
            "       g.name AS gym_name"
            " FROM memberships m JOIN gyms g ON g.id=m.gym_id"
            " WHERE m.user_id=? ORDER BY m.id DESC LIMIT 1", (user_id,)
        ).fetchone())


def list_pending_requests(gym_id: int) -> List[Dict[str, Any]]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT m.id, m.user_id, u.display_name, u.picture_url, m.created_at"
            " FROM memberships m"
            " LEFT JOIN users u ON u.line_user_id=m.user_id"
            " WHERE m.gym_id=? AND m.role='member' AND m.status='pending'"
            " ORDER BY m.id", (gym_id,)).fetchall()
    return [dict(r) for r in rows]


def list_members_for_trainer(trainer_id: str) -> List[Dict[str, Any]]:
    """担当トレーナーの active 会員一覧."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT m.id AS membership_id, m.user_id, m.gym_id,"
            "       u.display_name, u.picture_url"
            " FROM memberships m"
            " LEFT JOIN users u ON u.line_user_id=m.user_id"
            " WHERE m.trainer_id=? AND m.role='member' AND m.status='active'"
            " ORDER BY u.display_name", (trainer_id,)).fetchall()
    return [dict(r) for r in rows]


# ---- personas (ジムのAIコーチ) ----

def save_persona(*, gym_id: int, coach_name: str, system_prompt: str,
                 avatar_url: Optional[str] = None) -> None:
    with get_conn() as c:
        c.execute("""
            INSERT INTO personas (gym_id, coach_name, system_prompt, avatar_url)
            VALUES (?,?,?,?)
            ON CONFLICT(gym_id) DO UPDATE SET
              coach_name=excluded.coach_name,
              system_prompt=excluded.system_prompt,
              avatar_url=COALESCE(excluded.avatar_url, personas.avatar_url),
              updated_at=CURRENT_TIMESTAMP
        """, (gym_id, coach_name, system_prompt, avatar_url))


def get_persona(gym_id: int) -> Optional[Dict[str, Any]]:
    with get_conn() as c:
        return _d(c.execute(
            "SELECT * FROM personas WHERE gym_id=?", (gym_id,)
        ).fetchone())


# ---- comments ----

def add_comment(*, user_id: str, body: str, author_type: str,
                author_id: Optional[str] = None,
                entry_id: Optional[int] = None,
                target_date: Optional[str] = None,
                is_directive: bool = False) -> int:
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO comments"
            " (user_id, entry_id, target_date, author_type, author_id, body, is_directive)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, entry_id, target_date, author_type, author_id,
             body, 1 if is_directive else 0))
        return cur.lastrowid


def fetch_comments_for_user(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM comments WHERE user_id=?"
            " ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
    return [dict(r) for r in rows]


def fetch_active_directives(user_id: str, days: int = 28) -> List[Dict[str, Any]]:
    """AIコーチのコンテキストに載せる、トレーナーの「指導指示」."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT body, created_at FROM comments"
            " WHERE user_id=? AND author_type='trainer' AND is_directive=1"
            " AND created_at >= datetime('now', ?)"
            " ORDER BY created_at DESC", (user_id, f"-{days} days")).fetchall()
    return [dict(r) for r in rows]
