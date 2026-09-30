-- Phase 3.5: 目的設定（減量/減塩/筋肉増量）
CREATE TABLE IF NOT EXISTS goal_profiles (
    user_id          TEXT PRIMARY KEY,
    goal_mode        TEXT,              -- weight / salt / muscle
    target_weight_kg REAL,
    goal_days        INTEGER,
    calc_target_kcal REAL,              -- 計算された1日の目標摂取kcal
    salt_target_g    REAL,              -- 減塩モードの目標（既定 6.0g）
    protein_target_g REAL,              -- 筋肉モードのたんぱく質目標
    sex              TEXT,              -- male / female（推定計算用）
    age              INTEGER,
    height_cm        REAL,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
