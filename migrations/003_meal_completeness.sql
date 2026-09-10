-- 催促機能用: その日に「朝食を記録したか」を保持するフラグ
CREATE TABLE IF NOT EXISTS meal_completeness (
    user_id    TEXT NOT NULL,
    date       TEXT NOT NULL,
    breakfast  INTEGER NOT NULL DEFAULT 0,  -- 0=未記録, 1=記録済み
    lunch      INTEGER NOT NULL DEFAULT 0,
    dinner     INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, date)
);

CREATE INDEX IF NOT EXISTS idx_meal_comp_user_date
    ON meal_completeness(user_id, date);
