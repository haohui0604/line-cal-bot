-- AIひとことコメントのキャッシュ（同一集計値の再生成を防ぐ）
CREATE TABLE IF NOT EXISTS report_comments (
  cache_key   TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  scope       TEXT NOT NULL,          -- daily / weekly / monthly
  period_key  TEXT NOT NULL,          -- 2026-09-19 / 2026-W38 / 2026-09
  headline    TEXT NOT NULL,
  comment     TEXT NOT NULL,
  advice      TEXT NOT NULL,
  source      TEXT NOT NULL,          -- ai / rule / cache
  facts_json  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_comments_user ON report_comments(user_id, scope, period_key);
