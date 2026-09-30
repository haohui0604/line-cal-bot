-- Phase 3.5: トレーナー招待コード
CREATE TABLE IF NOT EXISTS trainer_invites (
    code        TEXT PRIMARY KEY,          -- 例: TR-AB12
    gym_id      INTEGER NOT NULL REFERENCES gyms(id),
    created_by  TEXT NOT NULL,             -- 発行したオーナー
    expires_at  TEXT NOT NULL,             -- ISO文字列（文字列比較で期限判定）
    used_by     TEXT,                      -- 使用したトレーナー（NULL=未使用）
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trainer_invites_gym ON trainer_invites(gym_id);
