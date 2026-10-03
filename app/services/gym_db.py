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
            " WHERE m.user_id=?"
            " ORDER BY CASE WHEN m.status='active' THEN 0 ELSE 1 END,"
            "          m.id DESC LIMIT 1", (user_id,)
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


def fetch_comments_for_user(user_id: str, limit: int = 50,
                           offset: int = 0) -> List[Dict[str, Any]]:
    """コメントを新しい順に取得（ページング用に offset を受ける）."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM comments WHERE user_id=?"
            " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, max(0, offset))).fetchall()
    return [dict(r) for r in rows]


def count_comments_for_user(user_id: str) -> int:
    with get_conn() as c:
        r = c.execute("SELECT COUNT(*) AS n FROM comments WHERE user_id=?",
                      (user_id,)).fetchone()
    return int((r["n"] if r else 0) or 0)


def fetch_active_directives(user_id: str, days: int = 28) -> List[Dict[str, Any]]:
    """AIコーチのコンテキストに載せる、トレーナーの「指導指示」."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT body, created_at FROM comments"
            " WHERE user_id=? AND author_type='trainer' AND is_directive=1"
            " AND created_at >= datetime('now', ?)"
            " ORDER BY created_at DESC", (user_id, f"-{days} days")).fetchall()
    return [dict(r) for r in rows]


# ---- スタッフ（トレーナー / ジム管理者）向けの参照・権限 (Phase 2) ----

def get_staff_memberships(user_id: str) -> List[Dict[str, Any]]:
    """そのユーザーが trainer / gym_admin として active な所属一覧."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT m.id, m.gym_id, m.role, g.name AS gym_name"
            " FROM memberships m JOIN gyms g ON g.id=m.gym_id"
            " WHERE m.user_id=? AND m.role IN ('trainer','gym_admin')"
            " AND m.status='active'", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def is_staff(user_id: str) -> bool:
    return len(get_staff_memberships(user_id)) > 0


def list_members_for_staff(staff_id: str) -> List[Dict[str, Any]]:
    """スタッフが見てよい会員一覧（自分の担当 + 自分がadminのジム全会員）."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT DISTINCT m.user_id, m.gym_id, m.trainer_id,
                   u.display_name, u.picture_url, g.name AS gym_name
            FROM memberships m
            JOIN gyms g ON g.id = m.gym_id
            LEFT JOIN users u ON u.line_user_id = m.user_id
            WHERE m.role='member' AND m.status='active' AND (
                m.trainer_id = ?
                OR m.gym_id IN (
                    SELECT gym_id FROM memberships
                    WHERE user_id=? AND role='gym_admin' AND status='active')
            )
            ORDER BY u.display_name
        """, (staff_id, staff_id)).fetchall()
    return [dict(r) for r in rows]


def can_staff_view_member(staff_id: str, member_id: str) -> bool:
    """会員詳細・コメント投稿の権限チェック."""
    with get_conn() as c:
        row = c.execute("""
            SELECT 1 FROM memberships m
            WHERE m.role='member' AND m.status='active' AND m.user_id=? AND (
                m.trainer_id = ?
                OR m.gym_id IN (
                    SELECT gym_id FROM memberships
                    WHERE user_id=? AND role='gym_admin' AND status='active')
            ) LIMIT 1
        """, (member_id, staff_id, staff_id)).fetchone()
    return row is not None


def list_pending_for_staff(staff_id: str) -> List[Dict[str, Any]]:
    """スタッフの所属ジムに届いている pending 入会申請一覧."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT m.id, m.user_id, m.gym_id, m.created_at,
                   u.display_name, u.picture_url, g.name AS gym_name
            FROM memberships m
            JOIN gyms g ON g.id = m.gym_id
            LEFT JOIN users u ON u.line_user_id = m.user_id
            WHERE m.role='member' AND m.status='pending' AND m.gym_id IN (
                SELECT gym_id FROM memberships
                WHERE user_id=? AND role IN ('trainer','gym_admin')
                AND status='active')
            ORDER BY m.id
        """, (staff_id,)).fetchall()
    return [dict(r) for r in rows]


def get_request_for_staff(staff_id: str, membership_id: int) -> Optional[Dict[str, Any]]:
    """承認/却下の権限チェックつきで申請を1件取得."""
    with get_conn() as c:
        row = c.execute("""
            SELECT m.*, g.name AS gym_name, u.display_name
            FROM memberships m
            JOIN gyms g ON g.id = m.gym_id
            LEFT JOIN users u ON u.line_user_id = m.user_id
            WHERE m.id=? AND m.role='member' AND m.status='pending'
              AND m.gym_id IN (
                SELECT gym_id FROM memberships
                WHERE user_id=? AND role IN ('trainer','gym_admin')
                AND status='active')
        """, (membership_id, staff_id)).fetchone()
    return _d(row)


# ---- トレーナー招待コード (Phase 3.5) ----

def create_trainer_invite(gym_id: int, created_by: str, days: int = 7) -> str:
    """トレーナー招待コードを発行 (例: TR-AB12)."""
    import secrets
    from datetime import datetime, timedelta, timezone
    code = "TR-" + secrets.token_hex(2).upper()
    exp = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with get_conn() as c:
        c.execute(
            "INSERT INTO trainer_invites (code, gym_id, created_by, expires_at)"
            " VALUES (?,?,?,?)", (code, gym_id, created_by, exp))
    return code


def use_trainer_invite(code: str, user_id: str) -> dict:
    """招待コードを使ってトレーナーとして登録."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as c:
        row = c.execute(
            "SELECT * FROM trainer_invites WHERE code=?",
            (code.upper(),)).fetchone()
        if not row:
            return {"result": "invalid"}
        r = dict(row)
        if r["used_by"]:
            return {"result": "used"}
        if r["expires_at"] < now:
            return {"result": "expired"}
        # すでにそのジムのトレーナーなら冪等で成功扱い
        existing = c.execute(
            "SELECT id, status FROM memberships"
            " WHERE gym_id=? AND user_id=? AND role='trainer'",
            (r["gym_id"], user_id)).fetchone()
        if existing:
            if existing["status"] == "active":
                return {"result": "already", "gym_id": r["gym_id"]}
            c.execute(
                "UPDATE memberships SET status='active',"
                " updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (existing["id"],))
        else:
            c.execute(
                "INSERT INTO memberships (gym_id, user_id, role, status)"
                " VALUES (?,?, 'trainer', 'active')",
                (r["gym_id"], user_id))
        c.execute(
            "UPDATE trainer_invites SET used_by=? WHERE code=?",
            (user_id, code.upper()))
        return {"result": "ok", "gym_id": r["gym_id"]}


def list_trainers(gym_id: int):
    with get_conn() as c:
        rows = c.execute(
            "SELECT m.user_id, u.display_name, u.picture_url, m.status"
            " FROM memberships m LEFT JOIN users u ON u.line_user_id=m.user_id"
            " WHERE m.gym_id=? AND m.role='trainer' AND m.status='active'"
            " ORDER BY m.id", (gym_id,)).fetchall()
    return [dict(r) for r in rows]


def fetch_comments_for_date(user_id: str, target_date: str):
    """その日付宛のコメント一覧（author名つき）."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT cm.*, u.display_name AS author_name FROM comments cm"
            " LEFT JOIN users u ON u.line_user_id=cm.author_id"
            " WHERE cm.user_id=? AND cm.target_date=?"
            " ORDER BY cm.created_at", (user_id, target_date)).fetchall()
    return [dict(r) for r in rows]


def fetch_past_trainer_comments(user_id: str, before_date: str,
                                days: int = 14, limit: int = 5):
    """対象日より「前」の日付に付いたトレーナーコメントを新しい順に返す。

    当日のコメントは含めない（その日のAIコメントがトレーナーの
    発言をなぞってしまうのを防ぐため）。
    """
    from datetime import date as _d, timedelta as _td
    try:
        base = _d.fromisoformat(str(before_date)[:10])
    except Exception:
        return []
    since = (base - _td(days=days)).isoformat()
    with get_conn() as c:
        rows = c.execute(
            "SELECT cm.*, u.display_name AS author_name FROM comments cm"
            " LEFT JOIN users u ON u.line_user_id=cm.author_id"
            " WHERE cm.user_id=? AND cm.author_type='trainer'"
            "   AND cm.target_date IS NOT NULL AND cm.target_date<>''"
            "   AND cm.target_date < ? AND cm.target_date >= ?"
            " ORDER BY cm.target_date DESC, cm.created_at DESC"
            " LIMIT ?", (user_id, str(before_date)[:10], since, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def get_member_trainer(user_id: str) -> Optional[str]:
    """会員の担当トレーナー（active 所属の trainer_id）を1件返す。未設定なら None."""
    from app.services.db import get_conn
    with get_conn() as c:
        r = c.execute(
            "SELECT trainer_id FROM memberships"
            " WHERE user_id=? AND role='member' AND status='active'"
            "   AND trainer_id IS NOT NULL ORDER BY id DESC LIMIT 1",
            (user_id,)).fetchone()
    return r["trainer_id"] if r else None


def list_members_for_gym(gym_id: int) -> List[Dict[str, Any]]:
    """ジムの active 会員一覧（オーナーの管理画面用）."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT m.id AS membership_id, m.user_id, m.trainer_id,
                   u.display_name, u.picture_url,
                   t.display_name AS trainer_name
            FROM memberships m
            LEFT JOIN users u ON u.line_user_id = m.user_id
            LEFT JOIN users t ON t.line_user_id = m.trainer_id
            WHERE m.gym_id=? AND m.role='member' AND m.status='active'
            ORDER BY u.display_name, m.id
        """, (gym_id,)).fetchall()
    return [dict(r) for r in rows]


def remove_member(membership_id: int, *, by_user_id: str) -> Dict[str, Any]:
    """オーナー（そのジムの gym_admin）による会員の解除。

    status='left' にし、担当トレーナーの紐づけも外す。食事・体重などの
    記録データ自体は削除しない（ジムとの紐づけだけを切る）。
    実行者がそのジムの gym_admin(active) でなければ forbidden を返す。
    """
    with get_conn() as c:
        row = c.execute(
            "SELECT m.id, m.gym_id, m.user_id, g.name AS gym_name"
            " FROM memberships m JOIN gyms g ON g.id=m.gym_id"
            " WHERE m.id=? AND m.role='member' AND m.status='active'",
            (membership_id,)).fetchone()
        if not row:
            return {"result": "not_found"}
        owner = c.execute(
            "SELECT 1 FROM memberships WHERE gym_id=? AND user_id=?"
            " AND role='gym_admin' AND status='active'",
            (row["gym_id"], by_user_id)).fetchone()
        if not owner:
            return {"result": "forbidden"}
        c.execute(
            "UPDATE memberships SET status='left', trainer_id=NULL,"
            " updated_at=CURRENT_TIMESTAMP WHERE id=?", (membership_id,))
    return {"result": "ok", "member_id": row["user_id"],
            "gym_id": row["gym_id"], "gym_name": row["gym_name"]}


# ---- ジム管理者画面 (Phase 5) ----

def get_gym(gym_id: int):
    with get_conn() as c:
        return _d(c.execute(
            "SELECT id, name, join_code, plan, owner_user_id, created_at,"
            "       deleted_at FROM gyms WHERE id=?", (gym_id,)).fetchone())


def list_admin_gyms(user_id: str):
    """そのユーザーが管理者を務める有効なジム一覧."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT m.gym_id, g.name AS gym_name, g.join_code
              FROM memberships m JOIN gyms g ON g.id = m.gym_id
             WHERE m.user_id=? AND m.role='gym_admin' AND m.status='active'
               AND g.deleted_at IS NULL
             ORDER BY m.id
        """, (user_id,)).fetchall()
    return [dict(r) for r in rows]


def list_gym_staff_with_status(gym_id: int):
    """ジムのトレーナー／ジム管理者を状態つきで一覧（active優先）."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT m.id AS membership_id, m.user_id, m.role, m.status,
                   u.display_name, u.picture_url,
                   (SELECT COUNT(*) FROM memberships mm
                     WHERE mm.gym_id=m.gym_id AND mm.role='member'
                       AND mm.status='active' AND mm.trainer_id=m.user_id) AS member_count
              FROM memberships m
              LEFT JOIN users u ON u.line_user_id = m.user_id
             WHERE m.gym_id=? AND m.role IN ('trainer','gym_admin')
             ORDER BY CASE m.status WHEN 'active' THEN 0 ELSE 1 END, m.id
        """, (gym_id,)).fetchall()
    return [dict(r) for r in rows]


def count_assigned_members(gym_id: int, trainer_id: str) -> int:
    with get_conn() as c:
        r = c.execute(
            "SELECT COUNT(*) AS n FROM memberships"
            " WHERE gym_id=? AND role='member' AND status='active'"
            "   AND trainer_id=?", (gym_id, trainer_id)).fetchone()
    return int(r["n"]) if r else 0


def remove_staff(membership_id: int, *, by_user_id: str) -> dict:
    """トレーナー／ジム管理者を解除。担当会員が残っていれば拒否する."""
    with get_conn() as c:
        row = c.execute(
            "SELECT id, gym_id, user_id, role, status FROM memberships WHERE id=?",
            (membership_id,)).fetchone()
        if not row:
            return {"ok": False, "reason": "not_found"}
        m = dict(row)
        if m["role"] not in ("trainer", "gym_admin"):
            return {"ok": False, "reason": "not_staff"}
        n = count_assigned_members(m["gym_id"], m["user_id"])
        if n > 0:
            return {"ok": False, "reason": "has_members", "count": n}
        c.execute("""
            UPDATE memberships
               SET status='left', removed_by=?, removed_at=CURRENT_TIMESTAMP,
                   removed_reason='staff_removed'
             WHERE id=?
        """, (by_user_id, membership_id))
        return {"ok": True}


def set_member_trainer(membership_id: int, trainer_id) -> bool:
    """会員の担当トレーナーを変更（空欄にする場合は None）."""
    with get_conn() as c:
        cur = c.execute(
            "UPDATE memberships SET trainer_id=?, updated_at=CURRENT_TIMESTAMP"
            " WHERE id=? AND role='member' AND status='active'",
            (trainer_id or None, membership_id))
        return (cur.rowcount or 0) > 0


def list_members_for_gym_filtered(gym_id: int, trainer_id=None):
    """ジムの active 会員一覧（trainer_id 指定で「自分の担当のみ」）."""
    sql = """
        SELECT m.id AS membership_id, m.user_id, m.trainer_id,
               u.display_name, u.picture_url, t.display_name AS trainer_name
          FROM memberships m
          LEFT JOIN users u ON u.line_user_id = m.user_id
          LEFT JOIN users t ON t.line_user_id = m.trainer_id
         WHERE m.gym_id=? AND m.role='member' AND m.status='active'
    """
    args = [gym_id]
    if trainer_id:
        sql += " AND m.trainer_id=?"
        args.append(trainer_id)
    sql += " ORDER BY u.display_name, m.id"
    with get_conn() as c:
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def list_my_member_comments(trainer_id: str, limit: int = 30, offset: int = 0):
    """自分が担当する会員からのコメント（新しい順）."""
    with get_conn() as c:
        rows = c.execute("""
            SELECT cm.id, cm.user_id, cm.body, cm.target_date, cm.created_at,
                   cm.author_type, u.display_name AS member_name
              FROM comments cm
              JOIN memberships m
                ON m.user_id = cm.user_id AND m.role='member'
               AND m.status='active' AND m.trainer_id=?
              LEFT JOIN users u ON u.line_user_id = cm.user_id
             WHERE cm.author_type='member'
             ORDER BY cm.created_at DESC, cm.id DESC
             LIMIT ? OFFSET ?
        """, (trainer_id, int(limit), int(offset))).fetchall()
    return [dict(r) for r in rows]


def count_my_member_comments(trainer_id: str) -> int:
    with get_conn() as c:
        r = c.execute("""
            SELECT COUNT(*) AS n
              FROM comments cm
              JOIN memberships m
                ON m.user_id = cm.user_id AND m.role='member'
               AND m.status='active' AND m.trainer_id=?
             WHERE cm.author_type='member'
        """, (trainer_id,)).fetchone()
    return int(r["n"]) if r else 0


# ================= Phase 6: 質問スレッド・未確認管理・ジム設定 =================

def _ensure_reply_support(c) -> None:
    """comments.reply_to_id / notified_at を冪等に用意する."""
    cols = {r[1] for r in c.execute("PRAGMA table_info(comments)").fetchall()}
    if "reply_to_id" not in cols:
        c.execute("ALTER TABLE comments ADD COLUMN reply_to_id INTEGER")
    if "notified_at" not in cols:
        c.execute("ALTER TABLE comments ADD COLUMN notified_at TEXT")


def add_reply(*, member_id: str, trainer_id: str, body: str,
              reply_to_id: Optional[int] = None,
              target_date: Optional[str] = None,
              is_directive: bool = False) -> int:
    """トレーナー→会員の返信を保存し、会員の未確認コメントを確認済みにする.

    notified_at は「スタッフが確認した時刻」。NULL は未確認を表す。
    """
    with get_conn() as c:
        _ensure_reply_support(c)
        cur = c.execute(
            "INSERT INTO comments (user_id, target_date, author_type, author_id,"
            " body, is_directive, reply_to_id) VALUES (?,?,?,?,?,?,?)",
            (member_id, target_date, "trainer", trainer_id, body,
             1 if is_directive else 0, reply_to_id))
        cid = cur.lastrowid
        if reply_to_id:
            c.execute("UPDATE comments SET notified_at=CURRENT_TIMESTAMP"
                      " WHERE id=? AND (notified_at IS NULL OR notified_at='')",
                      (reply_to_id,))
        else:
            c.execute("UPDATE comments SET notified_at=CURRENT_TIMESTAMP"
                      " WHERE user_id=? AND author_type='member'"
                      "   AND (notified_at IS NULL OR notified_at='')", (member_id,))
        return cid


def _unread_clause() -> str:
    return "(cm.notified_at IS NULL OR cm.notified_at='')"


def list_unread_member_comments(trainer_id: str, limit: int = 50):
    """自分が担当する会員からの未確認コメント（新しい順）."""
    with get_conn() as c:
        _ensure_reply_support(c)
        rows = c.execute(f"""
            SELECT cm.id, cm.user_id, cm.body, cm.created_at, cm.target_date,
                   u.display_name AS member_name
              FROM comments cm
              JOIN memberships m ON m.user_id=cm.user_id AND m.role='member'
               AND m.status='active' AND m.trainer_id=?
              LEFT JOIN users u ON u.line_user_id=cm.user_id
             WHERE cm.author_type='member' AND {_unread_clause()}
             ORDER BY cm.created_at DESC, cm.id DESC LIMIT ?
        """, (trainer_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


def count_unread_member_comments(trainer_id: str) -> int:
    with get_conn() as c:
        _ensure_reply_support(c)
        r = c.execute(f"""
            SELECT COUNT(*) AS n FROM comments cm
              JOIN memberships m ON m.user_id=cm.user_id AND m.role='member'
               AND m.status='active' AND m.trainer_id=?
             WHERE cm.author_type='member' AND {_unread_clause()}
        """, (trainer_id,)).fetchone()
    return int(r["n"] if r else 0)


def list_member_threads(trainer_id: str, limit: int = 20):
    """担当会員ごとの最新コメント・未確認件数（未確認が多い順）."""
    with get_conn() as c:
        _ensure_reply_support(c)
        rows = c.execute(f"""
            SELECT cm.user_id,
                   COALESCE(u.display_name, cm.user_id) AS member_name,
                   MAX(cm.created_at) AS last_at,
                   SUM(CASE WHEN cm.author_type='member' AND {_unread_clause()}
                            THEN 1 ELSE 0 END) AS unread,
                   COUNT(*) AS total
              FROM comments cm
              JOIN memberships m ON m.user_id=cm.user_id AND m.role='member'
               AND m.status='active' AND m.trainer_id=?
              LEFT JOIN users u ON u.line_user_id=cm.user_id
             GROUP BY cm.user_id
             ORDER BY unread DESC, last_at DESC LIMIT ?
        """, (trainer_id, int(limit))).fetchall()
    return [dict(r) for r in rows]


def fetch_member_thread(trainer_id: str, member_id: str, limit: int = 50):
    """自分が担当する会員とのやりとり（会員の質問＋自分の返信）を古い順で返す."""
    with get_conn() as c:
        _ensure_reply_support(c)
        ok = c.execute(
            "SELECT 1 FROM memberships WHERE user_id=? AND role='member'"
            " AND status='active' AND trainer_id=?", (member_id, trainer_id)).fetchone()
        if not ok:
            return []
        rows = c.execute("""
            SELECT cm.id, cm.author_type, cm.author_id, cm.body, cm.created_at,
                   cm.target_date, cm.reply_to_id, cm.is_directive, cm.notified_at,
                   COALESCE(u.display_name, cm.author_id) AS author_name
              FROM comments cm
              LEFT JOIN users u ON u.line_user_id=cm.author_id
             WHERE cm.user_id=? AND (cm.author_type='member' OR cm.author_id=?)
             ORDER BY cm.created_at DESC, cm.id DESC LIMIT ?
        """, (member_id, trainer_id, int(limit))).fetchall()
    return [dict(r) for r in reversed(rows)]


def mark_thread_read(trainer_id: str, member_id: str) -> int:
    """スレッドを開いた時点で会員の未確認コメントを確認済みにする."""
    with get_conn() as c:
        _ensure_reply_support(c)
        ok = c.execute(
            "SELECT 1 FROM memberships WHERE user_id=? AND role='member'"
            " AND status='active' AND trainer_id=?", (member_id, trainer_id)).fetchone()
        if not ok:
            return 0
        cur = c.execute("UPDATE comments SET notified_at=CURRENT_TIMESTAMP"
                        " WHERE user_id=? AND author_type='member'"
                        "   AND (notified_at IS NULL OR notified_at='')", (member_id,))
        return int(cur.rowcount or 0)


def rename_gym(gym_id: int, name: str) -> bool:
    with get_conn() as c:
        cur = c.execute("UPDATE gyms SET name=? WHERE id=?", (name, gym_id))
        return (cur.rowcount or 0) > 0


def regenerate_join_code(gym_id: int) -> Optional[str]:
    """入会コードを再発行する（古いコードは無効になる）."""
    with get_conn() as c:
        code = _unique_join_code(c)
        cur = c.execute("UPDATE gyms SET join_code=? WHERE id=?", (code, gym_id))
        if not (cur.rowcount or 0):
            return None
        return code
