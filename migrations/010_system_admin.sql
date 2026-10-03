-- システム管理者 / 監査ログ (010)
-- init_db は起動ごとに全ファイルを再実行するため、必ず IF NOT EXISTS で冪等にする。
-- 既存テーブルへの列追加は db.py の ADDED_COLUMNS 側で行う（ALTER は再実行で失敗するため）。

CREATE TABLE IF NOT EXISTS system_admins (
  user_id     TEXT PRIMARY KEY,
  note        TEXT,
  created_by  TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_user_id  TEXT,
  action         TEXT NOT NULL,
  target_type    TEXT,
  target_id      TEXT,
  detail         TEXT,
  created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_actor   ON audit_logs(actor_user_id);
