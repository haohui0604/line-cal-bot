"""システム管理者向けの永続化 (010_system_admin.sql).

- system_admins : システム管理者（環境変数 ADMIN_USER_IDS と併用）
- audit_logs    : 管理操作の記録
- gyms          : deleted_at による論理削除（会員の履歴は残す）
既存の get_conn() を使うので SQLite / Turso 両対応。
"""
import secrets
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services.db import get_conn


def _d(row) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


def _rows(rows) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


def _table_exists(c, name: str) -> bool:
    try:
        return c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None
    except Exception:
        return False


# ---------------- システム管理者 ----------------

def is_system_admin(user_id: str) -> bool:
    """環境変数 ADMIN_USER_IDS か system_admins テーブルに居れば True."""
    if not user_id:
        return False
    if user_id in settings.admin_user_id_set:
        return True
    with get_conn() as c:
        return c.execute("SELECT 1 FROM system_admins WHERE user_id=?",
                         (user_id,)).fetchone() is not None


def list_system_admins() -> List[Dict[str, Any]]:
    with get_conn() as c:
        return _rows(c.execute(
            "SELECT user_id, note, created_by, created_at "
            "FROM system_admins ORDER BY created_at").fetchall())


def add_system_admin(user_id: str, note: Optional[str] = None,
                     created_by: Optional[str] = None) -> None:
    with get_conn() as c:
        c.execute("""
            INSERT INTO system_admins (user_id, note, created_by)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              note = COALESCE(excluded.note, system_admins.note)
        """, (user_id, note, created_by))


def remove_system_admin(user_id: str) -> bool:
    """最後の1人は削除できない（env とテーブルの合計で判定）."""
    with get_conn() as c:
        table_ids = {r["user_id"] for r in
                     _rows(c.execute("SELECT user_id FROM system_admins").fetchall())}
        if not (table_ids - {user_id}) | set(settings.admin_user_id_set):
            return False
        cur = c.execute("DELETE FROM system_admins WHERE user_id=?", (user_id,))
        return (cur.rowcount or 0) > 0


def bootstrap_env_admins() -> int:
    """ADMIN_USER_IDS を system_admins へ取り込む（冪等・起動時に呼ぶ）."""
    n = 0
    for uid in sorted(settings.admin_user_id_set):
        with get_conn() as c:
            cur = c.execute("""
                INSERT INTO system_admins (user_id, note, created_by)
                VALUES (?, 'env:ADMIN_USER_IDS', 'system')
                ON CONFLICT(user_id) DO NOTHING
            """, (uid,))
            n += (cur.rowcount or 0)
    return n


# ---------------- 監査ログ ----------------

def add_audit(actor_user_id: str, action: str, target_type: str = "",
              target_id: str = "", detail: str = "") -> None:
    with get_conn() as c:
        c.execute("""
            INSERT INTO audit_logs (actor_user_id, action, target_type, target_id, detail)
            VALUES (?, ?, ?, ?, ?)
        """, (actor_user_id, action, target_type or None,
              target_id or None, detail or None))


def list_audit(limit: int = 50) -> List[Dict[str, Any]]:
    with get_conn() as c:
        return _rows(c.execute(
            "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall())


# ---------------- ジム ----------------

def list_gyms() -> List[Dict[str, Any]]:
    with get_conn() as c:
        return _rows(c.execute("""
            SELECT g.id, g.name, g.join_code, g.plan, g.owner_user_id,
                   g.created_at, g.deleted_at,
                   (SELECT COUNT(*) FROM memberships m
                     WHERE m.gym_id=g.id AND m.role='member'
                       AND m.status='active') AS members,
                   (SELECT COUNT(*) FROM memberships m
                     WHERE m.gym_id=g.id AND m.role='trainer'
                       AND m.status='active') AS trainers,
                   (SELECT COUNT(*) FROM memberships m
                     WHERE m.gym_id=g.id AND m.role='gym_admin'
                       AND m.status='active') AS admins
            FROM gyms g
            ORDER BY (g.deleted_at IS NOT NULL), g.id DESC
        """).fetchall())


def create_gym(name: str, owner_user_id: Optional[str] = None) -> Dict[str, Any]:
    """ジムを作成する。

    gyms のスキーマは環境によって差がある（plan_expires_at が NOT NULL など）ため、
    実テーブルの列を調べてから、存在する列だけを埋める。
    """
    code = "GYM-" + secrets.token_hex(3).upper()
    with get_conn() as c:
        have = {r[1] for r in c.execute("PRAGMA table_info(gyms)").fetchall()}
        pairs, params = [], []
        for col, val in (("name", name), ("join_code", code),
                         ("plan", "free"), ("owner_user_id", owner_user_id)):
            if col in have:
                # owner_user_id は NOT NULL の環境があるため空文字で埋める
                pairs.append((col, "?")); params.append(val if val is not None else "")
        if "plan_expires_at" in have:
            # NOT NULL でも通るよう SQL 式で埋める
            pairs.append(("plan_expires_at", "datetime('now','+365 days')"))
        cols = ", ".join(p[0] for p in pairs)
        marks = ", ".join(p[1] for p in pairs)
        cur = c.execute(f"INSERT INTO gyms ({cols}) VALUES ({marks})", params)
        gid = cur.lastrowid
    return {"id": gid, "name": name, "join_code": code}


def soft_delete_gym(gym_id: int, actor_user_id: str, reason: str = "") -> bool:
    """ジムを論理削除。所属メンバーの担当トレーナーは空欄にする（要件どおり）."""
    with get_conn() as c:
        c.execute("UPDATE gyms SET deleted_at=CURRENT_TIMESTAMP, deleted_by=? "
                  "WHERE id=?", (actor_user_id, gym_id))
        c.execute("""
            UPDATE memberships
               SET status='left', trainer_id=NULL,
                   removed_by=?, removed_at=CURRENT_TIMESTAMP, removed_reason=?
             WHERE gym_id=? AND status='active'
        """, (actor_user_id, reason or "gym_deleted", gym_id))
        return c.execute("SELECT deleted_at FROM gyms WHERE id=?",
                         (gym_id,)).fetchone() is not None


def list_gym_staff(gym_id: int) -> List[Dict[str, Any]]:
    with get_conn() as c:
        return _rows(c.execute("""
            SELECT m.id, m.user_id, m.role, m.status, m.trainer_id,
                   u.display_name
              FROM memberships m
              LEFT JOIN users u ON u.line_user_id = m.user_id
             WHERE m.gym_id=? AND m.role IN ('gym_admin','trainer')
             ORDER BY m.role, m.id
        """, (gym_id,)).fetchall())


def remove_membership(membership_id: int, actor_user_id: str,
                      reason: str = "") -> bool:
    with get_conn() as c:
        row = c.execute("SELECT id FROM memberships WHERE id=?",
                        (membership_id,)).fetchone()
        if row is None:
            return False
        c.execute("""
            UPDATE memberships
               SET status='left', trainer_id=NULL,
                   removed_by=?, removed_at=CURRENT_TIMESTAMP, removed_reason=?
             WHERE id=?
        """, (actor_user_id, reason or "removed", membership_id))
        return True


def create_invite(gym_id: int, created_by: str, role: str = "trainer",
                  days: int = 7) -> str:
    """招待コードを発行（role='gym_admin' なら GA- 接頭辞）."""
    code = ("GA-" if role == "gym_admin" else "TR-") + secrets.token_hex(3).upper()
    with get_conn() as c:
        try:
            c.execute("""
                INSERT INTO trainer_invites (code, gym_id, created_by, expires_at, role)
                VALUES (?, ?, ?, datetime('now', ?), ?)
            """, (code, gym_id, created_by, f"+{int(days)} days", role))
        except Exception:
            c.execute("""
                INSERT INTO trainer_invites (code, gym_id, created_by, expires_at)
                VALUES (?, ?, ?, datetime('now', ?))
            """, (code, gym_id, created_by, f"+{int(days)} days"))
    return code


# ---------------- 分析 ----------------

def analytics() -> Dict[str, Any]:
    """運営KPI。entries/comments が無い環境でも落ちないようにガードする."""
    out: Dict[str, Any] = {}
    with get_conn() as c:
        def one(sql: str, args=()) -> int:
            r = c.execute(sql, args).fetchone()
            if r is None:
                return 0
            try:
                return int(r["n"])
            except Exception:
                return int(r[0])

        out["gyms_total"] = one("SELECT COUNT(*) AS n FROM gyms")
        out["gyms_active"] = one("SELECT COUNT(*) AS n FROM gyms WHERE deleted_at IS NULL")
        out["users_total"] = one("SELECT COUNT(*) AS n FROM users")
        out["members_active"] = one("SELECT COUNT(*) AS n FROM memberships "
                                    "WHERE role='member' AND status='active'")
        out["trainers_active"] = one("SELECT COUNT(*) AS n FROM memberships "
                                     "WHERE role='trainer' AND status='active'")
        out["admins_active"] = one("SELECT COUNT(*) AS n FROM memberships "
                                   "WHERE role='gym_admin' AND status='active'")
        out["pending_requests"] = one("SELECT COUNT(*) AS n FROM memberships "
                                      "WHERE role='member' AND status='pending'")
        if _table_exists(c, "entries"):
            out["meals_7d"] = one("SELECT COUNT(*) AS n FROM entries "
                                  "WHERE date >= date('now','-7 days')")
            out["meals_30d"] = one("SELECT COUNT(*) AS n FROM entries "
                                   "WHERE date >= date('now','-30 days')")
            out["active_users_7d"] = one("SELECT COUNT(DISTINCT user_id) AS n "
                                         "FROM entries WHERE date >= date('now','-7 days')")
        if _table_exists(c, "comments"):
            out["comments_30d"] = one("SELECT COUNT(*) AS n FROM comments "
                                      "WHERE created_at >= datetime('now','-30 days')")

    out["gyms_needing_attention"] = [
        {"id": g["id"], "name": g["name"], "members": g["members"],
         "trainers": g["trainers"]}
        for g in list_gyms()
        if not g["deleted_at"] and (g["members"] == 0 or g["trainers"] == 0)
    ]
    return out
