-- 目標摂取カロリー (goals) テーブル
-- 001_init.sql では作成されていないため、追加する
CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    date        TEXT NOT NULL,
    target_kcal REAL NOT NULL,
    note        TEXT,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_goals_user_date
    ON goals(user_id, date);
