-- Phase 1: ジム / ユーザー / 担当紐づけ / AIコーチ / コメント
-- 既存の entries / activity / weight_logs 等には手を付けない

-- 全ロール共通のユーザー（LINE user ID は Messaging API と LINEログインで同一）
CREATE TABLE IF NOT EXISTS users (
    line_user_id TEXT PRIMARY KEY,
    display_name TEXT,
    picture_url  TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ジム
CREATE TABLE IF NOT EXISTS gyms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    join_code       TEXT NOT NULL UNIQUE,      -- 例: GYM-5821
    plan            TEXT NOT NULL DEFAULT 'trial',  -- trial/basic/standard/pro（将来の課金用）
    plan_expires_at TIMESTAMP,                  -- 手動請求の期限管理用
    owner_user_id   TEXT NOT NULL REFERENCES users(line_user_id),
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 「誰が・どのジムの・何として・誰の担当か」を1本に集約
CREATE TABLE IF NOT EXISTS memberships (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    gym_id      INTEGER NOT NULL REFERENCES gyms(id),
    user_id     TEXT NOT NULL REFERENCES users(line_user_id),
    role        TEXT NOT NULL,                    -- member / trainer / gym_admin
    trainer_id  TEXT REFERENCES users(line_user_id),  -- role=member のときの担当
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending / active / rejected / left
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(gym_id, user_id, role)
);

CREATE INDEX IF NOT EXISTS idx_memberships_user    ON memberships(user_id, status);
CREATE INDEX IF NOT EXISTS idx_memberships_trainer ON memberships(trainer_id, status);
CREATE INDEX IF NOT EXISTS idx_memberships_gym     ON memberships(gym_id, role, status);

-- ジム単位のAIコーチ人格（既存 user_settings のユーザー個人人格とは別）
CREATE TABLE IF NOT EXISTS personas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    gym_id        INTEGER NOT NULL UNIQUE REFERENCES gyms(id),
    coach_name    TEXT NOT NULL DEFAULT 'AIコーチ',
    system_prompt TEXT NOT NULL DEFAULT '',
    avatar_url    TEXT,                            -- Phase 2 のアバター生成用（当面 NULL）
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- トレーナー / AIコーチのコメント
CREATE TABLE IF NOT EXISTS comments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,                    -- 対象会員
    entry_id     INTEGER,                          -- 特定の食事記録へのコメント（任意）
    target_date  TEXT,                             -- 日次まとめコメント用（任意）
    author_type  TEXT NOT NULL,                    -- trainer / ai_coach
    author_id    TEXT,                             -- author_type=trainer のときのLINE ID
    body         TEXT NOT NULL,
    is_directive INTEGER NOT NULL DEFAULT 0,       -- 1=AIコーチの今後の指導に反映する指示
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_comments_user      ON comments(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comments_directive ON comments(user_id, author_type, is_directive);
