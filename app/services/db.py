"""SQLite / Turso(libsql) 永続化."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, List, Dict, Any
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class _CompatCursor:
    """libsql のカーソルを sqlite3.Row 互換(名前アクセス可)に変換するラッパー."""

    def __init__(self, cur):
        self._cur = cur
        desc = getattr(cur, "description", None)
        self._cols = [d[0] for d in desc] if desc else []

    @property
    def description(self):
        """列情報を素通しする（PRAGMA が空でも SELECT から列名を取れるように）."""
        return getattr(self._cur, "description", None)

    def _refresh_cols(self):
        """PRAGMA など description が遅れて入る文に備えて列名を取り直す."""
        if not self._cols:
            desc = getattr(self._cur, "description", None)
            if desc:
                self._cols = [d[0] for d in desc]

    def fetchone(self):
        self._refresh_cols()
        row = self._cur.fetchone()
        if row is None:
            return None
        if not self._cols:
            return row
        return dict(zip(self._cols, row))

    def fetchall(self):
        self._refresh_cols()
        rows = self._cur.fetchall()
        if not self._cols:
            return list(rows)
        return [dict(zip(self._cols, r)) for r in rows]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid


class _CompatConn:
    """libsql 接続を sqlite3.Connection 風に包むラッパー."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _CompatCursor(self._conn.execute(sql, params))

    def executescript(self, sql):
        return self._conn.executescript(sql)

    def commit(self):
        return self._conn.commit()

    def sync(self):
        return self._conn.sync()

    def close(self):
        return self._conn.close()


@contextmanager
def get_conn():
    if settings.TURSO_DATABASE_URL:
        import libsql
        raw = libsql.connect(
            "replica.db",
            sync_url=settings.TURSO_DATABASE_URL,
            auth_token=settings.TURSO_AUTH_TOKEN,
        )
        conn = _CompatConn(raw)
    else:
        conn = sqlite3.connect(settings.DB_PATH)
        conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
        if settings.TURSO_DATABASE_URL:
            conn.sync()   # 書き込みをTursoへ送信
    finally:
        conn.close()


# SQLite には ALTER TABLE ... ADD COLUMN IF NOT EXISTS が無いため、
# 後から足したカラムはここで「無いときだけ足す」形で冪等に適用する。
# init_db はデプロイやテストのたびに何度も呼ばれるので、
# 2回目以降に落ちてはいけない（migrations/*.sql の ALTER は再実行で失敗する）。
ADDED_COLUMNS = {
    # 目標PFCのマスタ（たんぱく質は 008、脂質・炭水化物を後から追加）
    "goal_profiles": {"fat_target_g": "REAL", "carb_target_g": "REAL"},
    # --- システム管理 (010) ---
    "system_admins": {"created_by": "TEXT"},
    # ジムの論理削除（履歴は残す）
    "gyms": {"deleted_at": "TEXT", "deleted_by": "TEXT"},
    # 除籍・担当解除の記録
    "memberships": {"removed_by": "TEXT", "removed_at": "TEXT",
                    "removed_reason": "TEXT"},
    # コメントのスレッド化（ユーザ返信→担当トレーナー通知）
    "comments": {"reply_to_id": "INTEGER", "notified_at": "TEXT"},
    # 招待コードの用途（trainer / gym_admin）
    "trainer_invites": {"role": "TEXT DEFAULT 'trainer'"},
}


def table_columns(conn, table: str) -> set:
    """テーブルのカラム名集合を返す。PRAGMA が使えない環境(libsql等)でも動く.

    1) SELECT * FROM t LIMIT 0 の description（libsql でも取得できる）
    2) PRAGMA table_info(t)（sqlite3 / sqlite3.Row / tuple / dict すべて許容）
    3) どちらも取れなければ空集合（＝不明。呼び出し側は「無い」前提で動く）
    """
    try:
        cur = conn.execute(f"SELECT * FROM {table} LIMIT 0")
        desc = getattr(cur, "description", None)
        if desc:
            names = set()
            for d in desc:
                try:
                    names.add(d[0])
                except Exception:
                    pass
            if names:
                return names
    except Exception:
        pass
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception:
        return set()
    out = set()
    for r in rows:
        if isinstance(r, dict):
            if "name" in r:
                out.add(r["name"])
            continue
        try:
            out.add(r["name"])          # sqlite3.Row
        except Exception:
            try:
                out.add(r[1])           # tuple
            except Exception:
                pass
    return out


def _ensure_columns(c) -> None:
    """定義にあって実テーブルに無いカラムだけを追加する.

    カラム判定は table_columns() に一本化し、PRAGMA が使えない
    Turso/libsql でもスキーマのズレを自動修復できるようにする。
    判定できない場合は「無い」とみなして ALTER を試し、
    既存カラムの重複エラーは握りつぶす（冪等）。
    """
    for table, cols in ADDED_COLUMNS.items():
        have = table_columns(c, table)
        for name, decl in cols.items():
            if name in have:
                continue
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                logger.info("added column: %s.%s", table, name)
            except Exception:
                logger.warning("ALTER TABLE skipped (already exists?): %s.%s",
                               table, name)



def init_db():
    """migrations/*.sql をファイル名順(001→002→…)に冪等適用する."""
    import glob
    mig_dir = Path(__file__).resolve().parents[2] / "migrations"
    files = sorted(glob.glob(str(mig_dir / "*.sql")))
    if not files:
        logger.warning("no migration files found under %s", mig_dir)
        return
    with get_conn() as c:
        for f in files:
            try:
                c.executescript(Path(f).read_text(encoding="utf-8"))
            except Exception:
                # 1ファイルの失敗で残り（010など）が適用されない事態を防ぐ
                logger.exception("migration failed: %s", Path(f).name)
        try:
            _ensure_columns(c)
        except Exception:
            logger.exception("_ensure_columns failed")
    logger.info("migrations applied: %s", [Path(f).name for f in files])


def save_entry(*, user_id: str, date: str, meal_slot: str, food_name: str,
               kcal: float, protein_g=None, fat_g=None, carb_g=None,
               salt_g=None, quantity_g=None, source_type: str,
               confidence: str, linked_image_url=None, note=None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO entries
            (user_id, date, meal_slot, food_name, kcal, protein_g, fat_g,
             carb_g, salt_g, quantity_g, source_type, confidence,
             linked_image_url, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, date, meal_slot, food_name) DO UPDATE SET
              kcal=excluded.kcal, protein_g=excluded.protein_g,
              fat_g=excluded.fat_g, carb_g=excluded.carb_g,
              salt_g=excluded.salt_g, quantity_g=excluded.quantity_g,
              source_type=excluded.source_type, confidence=excluded.confidence,
              updated_at=CURRENT_TIMESTAMP
        """, (user_id, date, meal_slot, food_name, kcal, protein_g,
              fat_g, carb_g, salt_g, quantity_g, source_type, confidence,
              linked_image_url, note))


def save_activity(*, user_id: str, date: str, total_kcal: float,
                  active_kcal=None, resting_kcal=None, source_type: str,
                  ocr_image_url=None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO activity
            (user_id, date, total_kcal, active_kcal, resting_kcal,
             source_type, ocr_image_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
              total_kcal=excluded.total_kcal, active_kcal=excluded.active_kcal,
              resting_kcal=excluded.resting_kcal, source_type=excluded.source_type
        """, (user_id, date, total_kcal, active_kcal, resting_kcal,
              source_type, ocr_image_url))


def save_weight(*, user_id: str, date: str, weight_kg: float, is_measured: int,
                body_fat_pct=None, muscle_kg=None, bmr_kcal=None, note=None):
    with get_conn() as c:
        c.execute("""
            INSERT INTO weight_logs
            (user_id, date, weight_kg, is_measured, body_fat_pct, muscle_kg,
             bmr_kcal, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
              weight_kg=excluded.weight_kg, is_measured=excluded.is_measured,
              body_fat_pct=excluded.body_fat_pct, muscle_kg=excluded.muscle_kg,
              bmr_kcal=excluded.bmr_kcal
        """, (user_id, date, weight_kg, is_measured, body_fat_pct,
              muscle_kg, bmr_kcal, note))


def fetch_day_summary(user_id: str, date: str) -> Dict[str, Any]:
    with get_conn() as c:
        r = c.execute("""
            SELECT COALESCE(SUM(kcal),0) AS intake_kcal,
                   COALESCE(SUM(protein_g),0) AS protein_g,
                   COALESCE(SUM(fat_g),0) AS fat_g,
                   COALESCE(SUM(carb_g),0) AS carb_g,
                   COALESCE(SUM(salt_g),0) AS salt_g
            FROM entries WHERE user_id=? AND date=?
        """, (user_id, date)).fetchone()
        a = c.execute("""
            SELECT total_kcal FROM activity
            WHERE user_id=? AND date=?
        """, (user_id, date)).fetchone()
    intake = r["intake_kcal"]
    burn = a["total_kcal"] if a else 0
    return {
        "date": date,
        "intake_kcal": intake,
        "burn_kcal": burn,
        "deficit_kcal": burn - intake,
        "protein_g": r["protein_g"],
        "fat_g": r["fat_g"],
        "carb_g": r["carb_g"],
        "salt_g": r["salt_g"],
    }


def fetch_today_food_names(user_id: str, date: str) -> List[str]:
    """当日に記録済みの食品名リスト (LLMコンテキスト用)."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT food_name FROM entries WHERE user_id=? AND date=? ORDER BY id",
            (user_id, date),
        ).fetchall()
    return [r["food_name"] for r in rows]


def fetch_recent_history(user_id: str, days: int = 7) -> List[Dict[str, Any]]:
    with get_conn() as c:
        rows = c.execute(
            """
            SELECT d.date,
                   COALESCE((SELECT SUM(kcal) FROM entries
                              WHERE user_id=? AND date=d.date), 0) AS intake_kcal,
                   COALESCE((SELECT total_kcal FROM activity
                              WHERE user_id=? AND date=d.date), 0) AS consumed_kcal
            FROM (
                SELECT date FROM entries  WHERE user_id=?
                UNION
                SELECT date FROM activity WHERE user_id=?
            ) d
            ORDER BY d.date DESC
            LIMIT ?
            """,
            (user_id, user_id, user_id, user_id, days),
        ).fetchall()

    def _get(r, key, idx):
        # libsql=辞書風 / sqlite3.Row=両対応 / 素のtuple=位置アクセス
        try:
            return r[key]
        except (TypeError, KeyError, IndexError):
            return r[idx]

    return [
        {"date": _get(r, "date", 0),
         "intake_kcal": _get(r, "intake_kcal", 1),
         "consumed_kcal": _get(r, "consumed_kcal", 2),
         "deficit_kcal": _get(r, "consumed_kcal", 2) - _get(r, "intake_kcal", 1)}
        for r in rows
    ]


# ---- goals (目標摂取カロリー) ----

def set_goal(*, user_id: str, date: str, target_kcal: float,
             note: Optional[str] = None):
    """その日の目標摂取 kcal を upsert."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO goals (user_id, date, target_kcal, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, date) DO UPDATE SET
              target_kcal=excluded.target_kcal,
              note=excluded.note
        """, (user_id, date, target_kcal, note))


def get_goal(user_id: str, date: str) -> Optional[float]:
    """指定日以前で最新の目標 kcal。未設定なら None。"""
    with get_conn() as c:
        r = c.execute(
            "SELECT target_kcal FROM goals"
            " WHERE user_id=? AND date<=? ORDER BY date DESC LIMIT 1",
            (user_id, date),
        ).fetchone()
    return r["target_kcal"] if r else None


def fetch_remaining_kcal(user_id: str, date: str) -> Optional[float]:
    """目標 - 摂取済み。目標未設定なら None。"""
    target = get_goal(user_id, date)
    if target is None:
        return None
    s = fetch_day_summary(user_id, date)
    return target - s["intake_kcal"]


# ---- 過去データ修正 ----

def update_entry_kcal(*, user_id: str, entry_id: Optional[int] = None,
                      date: Optional[str] = None,
                      meal_slot: Optional[str] = None,
                      food_name: Optional[str] = None,
                      new_kcal: float) -> bool:
    """entries の kcal を更新する.

    entry_id 指定なら一意に更新。そうでなければ完全一致 (date, meal_slot,
    food_name) で更新。戻り値: 更新できたら True。
    """
    with get_conn() as c:
        if entry_id is not None:
            cur = c.execute(
                "UPDATE entries SET kcal=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE id=? AND user_id=?",
                (new_kcal, entry_id, user_id),
            )
        else:
            cur = c.execute(
                "UPDATE entries SET kcal=?, updated_at=CURRENT_TIMESTAMP"
                " WHERE user_id=? AND date=? AND meal_slot=? AND food_name=?",
                (new_kcal, user_id, date, meal_slot, food_name),
            )
        return cur.rowcount > 0


def find_entry_candidates(*, user_id: str, date: str,
                          meal_slot: Optional[str] = None,
                          food_name_like: Optional[str] = None,
                          ) -> List[Dict[str, Any]]:
    """修正コマンドの候補検索.

    food_name_like 指定時は部分一致 (LIKE %...%)。
    部分一致で0件の場合は呼び出し側で日付のみの再検索を想定。
    """
    sql = ("SELECT id, date, meal_slot, food_name, kcal"
           " FROM entries WHERE user_id=? AND date=?")
    params: list = [user_id, date]
    if meal_slot:
        sql += " AND meal_slot=?"
        params.append(meal_slot)
    if food_name_like:
        sql += " AND (food_name LIKE ? OR ? LIKE '%' || food_name || '%')"
        params.append(f"%{food_name_like}%")
        params.append(food_name_like)
    sql += " ORDER BY meal_slot, id"
    with get_conn() as c:
        rows = c.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def fetch_latest_weight(user_id: str, on_or_before: str = None):
    """指定日以前の最新体重を返す（未入力日の繰越し補完用）."""
    sql = ("SELECT date, weight_kg, is_measured FROM weight_logs"
           " WHERE user_id=?")
    params = [user_id]
    if on_or_before:
        sql += " AND date<=?"
        params.append(on_or_before)
    sql += " ORDER BY date DESC LIMIT 1"
    with get_conn() as c:
        r = c.execute(sql, params).fetchone()
    return dict(r) if r else None


def fetch_weight_series(user_id: str, days: int = 7):
    """直近days日分の体重系列。未入力日は直前の値(過去日も含む)で繰越."""
    from datetime import date as _date, timedelta
    with get_conn() as c:
        rows = c.execute(
            "SELECT date, weight_kg, is_measured FROM weight_logs"
            " WHERE user_id=? ORDER BY date", (user_id,)).fetchall()
    by_date = {r["date"]: dict(r) for r in rows}

    from app.services.dates import today_jst_date
    end = today_jst_date()
    window_start = end - timedelta(days=days - 1)
    result, last = [], None

    # ウィンドウ外の最新値を carryover の起点にする
    for d in sorted(by_date.keys()):
        if d >= window_start.isoformat():
            break
        last = by_date[d]

    # ウィンドウ内を chronological に走査
    for i in range(days - 1, -1, -1):
        d = (end - timedelta(days=i)).isoformat()
        if d in by_date:
            last = by_date[d]
            result.append({"date": d, "weight_kg": last["weight_kg"],
                           "is_measured": last["is_measured"]})
        elif last is not None:
            result.append({"date": d, "weight_kg": last["weight_kg"],
                           "is_measured": 0})
    return result


def fetch_entries_for_date(user_id: str, date: str) -> List[Dict[str, Any]]:
    """指定日の食事明細を返す（AIコンテキスト注入用）."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT id, meal_slot, food_name, kcal, protein_g, fat_g, carb_g, salt_g, note"
            " FROM entries WHERE user_id=? AND date=?"
            " ORDER BY CASE meal_slot"
            "  WHEN 'breakfast' THEN 1 WHEN 'lunch' THEN 2"
            "  WHEN 'dinner' THEN 3 ELSE 4 END, id",
            (user_id, date)).fetchall()
    return [dict(r) for r in rows]


# ---- 人格設定 (user_settings) ----

_PERSONA_DEFAULTS = {
    "bot_name": "アシスタント",
    "bot_tone": "",
    "bot_pronoun": "わたし",
}


def save_user_persona(user_id: str, bot_name: str,
                      bot_tone: str, bot_pronoun: str) -> None:
    """人格データを upsert で保存."""
    with get_conn() as c:
        c.execute("""
            INSERT INTO user_settings
              (user_id, bot_name, bot_tone, bot_pronoun, updated_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
              bot_name=excluded.bot_name,
              bot_tone=excluded.bot_tone,
              bot_pronoun=excluded.bot_pronoun,
              updated_at=CURRENT_TIMESTAMP
        """, (user_id, bot_name, bot_tone, bot_pronoun))


def load_user_persona(user_id: str) -> dict:
    """人格データを読み出す（未設定ならデフォルト値）."""
    with get_conn() as c:
        r = c.execute(
            "SELECT bot_name, bot_tone, bot_pronoun"
            " FROM user_settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not r:
        return dict(_PERSONA_DEFAULTS)
    try:
        return {"bot_name": r["bot_name"], "bot_tone": r["bot_tone"],
                "bot_pronoun": r["bot_pronoun"]}
    except (TypeError, KeyError, IndexError):
        return {"bot_name": r[0], "bot_tone": r[1], "bot_pronoun": r[2]}

# ---- 記録単位の一覧・削除 ----

def fetch_recent_entries(user_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """新しい順に記録単位で返す（履歴表示・削除対象の特定用）."""
    with get_conn() as c:
        rows = c.execute(
            "SELECT id, date, meal_slot, food_name, kcal, protein_g, fat_g,"
            " carb_g, salt_g, source_type, created_at"
            " FROM entries WHERE user_id=?"
            " ORDER BY date DESC, CASE meal_slot"
            "  WHEN 'breakfast' THEN 1 WHEN 'lunch' THEN 2"
            "  WHEN 'dinner' THEN 3 ELSE 4 END, id DESC"
            " LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_entry(user_id: str, entry_id: int) -> bool:
    """1件削除。削除できたら True."""
    with get_conn() as c:
        cur = c.execute(
            "DELETE FROM entries WHERE id=? AND user_id=?",
            (entry_id, user_id),
        )
        return cur.rowcount > 0


def delete_entries_by_date(user_id: str, date: str) -> int:
    """指定日の食事記録をまとめて削除し、削除件数を返す."""
    with get_conn() as c:
        cur = c.execute(
            "DELETE FROM entries WHERE user_id=? AND date=?",
            (user_id, date),
        )
        return cur.rowcount
# ===================== レポート用 =====================

def fetch_day_meals(user_id: str, date: str) -> dict:
    """指定日の食事をスロット別に返す（今日のレポートの内訳用）。
    例: {"breakfast": {"food_name": "食パン", "kcal": 250}, ...}"""
    with get_conn() as c:
        rows = c.execute(
            "SELECT meal_slot, food_name, SUM(kcal) AS kcal FROM entries"
            " WHERE user_id=? AND date=? GROUP BY meal_slot, food_name",
            (user_id, date),
        ).fetchall()
    out = {}
    for r in rows:
        slot, name, kcal = r["meal_slot"], r["food_name"], r["kcal"]
        if slot not in out:
            out[slot] = {"food_name": name, "kcal": kcal}
        else:
            out[slot]["food_name"] += "・" + name
            out[slot]["kcal"] += kcal
    return out


def fetch_day_activity_total(user_id: str, date: str) -> float:
    """指定日の消費カロリー"""
    with get_conn() as c:
        r = c.execute(
            "SELECT COALESCE(total_kcal,0) AS t FROM activity "
            "WHERE user_id=? AND date=?",
            (user_id, date),
        ).fetchone()

    return r["t"] if r else 0


# ============== AIコメントのキャッシュ ==============

def get_report_comment(cache_key: str):
    """キャッシュ済みの AI コメントを返す。無ければ None。"""
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT headline, comment, advice, source FROM report_comments WHERE cache_key = ?",
            (cache_key,),
        )
        row = cur.fetchone()
    if not row:
        return None
    if isinstance(row, dict):
        return {"headline": row["headline"], "comment": row["comment"],
                "advice": row["advice"], "source": row["source"]}
    return {"headline": row[0], "comment": row[1], "advice": row[2], "source": row[3]}


def save_report_comment(cache_key: str, user_id: str, scope: str, period_key: str,
                        headline: str, comment: str, advice: str,
                        source: str, facts_json: str) -> None:
    """生成結果をキャッシュに保存（同一キーは上書き）。"""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO report_comments
               (cache_key, user_id, scope, period_key, headline, comment, advice, source, facts_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                 headline=excluded.headline, comment=excluded.comment, advice=excluded.advice,
                 source=excluded.source, facts_json=excluded.facts_json, created_at=excluded.created_at""",
            (cache_key, user_id, scope, period_key, headline, comment, advice, source, facts_json, now),
        )
        conn.commit()


def purge_report_comments(user_id: str, scope: str | None = None) -> int:
    """再生成したいとき用（プロンプト変更時に呼ぶ）。"""
    sql = "DELETE FROM report_comments WHERE user_id = ?"
    args = [user_id]
    if scope:
        sql += " AND scope = ?"
        args.append(scope)
    with get_conn() as conn:
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        return cur.rowcount


def update_entry_note(user_id: str, entry_id: int, note: str) -> bool:
    """記録の自分メモ(note)を更新。所有者チェックは WHERE user_id が担う."""
    with get_conn() as c:
        cur = c.execute(
            "UPDATE entries SET note=?, updated_at=CURRENT_TIMESTAMP"
            " WHERE id=? AND user_id=?", (note, entry_id, user_id))
        return cur.rowcount > 0


def fetch_entry_for_user(user_id: str, entry_id: int):
    """本人の記録を1件取得（質問の文脈付け用）。無ければ None."""
    with get_conn() as c:
        r = c.execute(
            "SELECT id, user_id, date, meal_slot, food_name, kcal, note"
            " FROM entries WHERE id=? AND user_id=?",
            (entry_id, user_id)).fetchone()
    return dict(r) if r else None
