CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    date            TEXT NOT NULL,
    meal_slot       TEXT NOT NULL,
    food_name       TEXT NOT NULL,
    kcal            REAL NOT NULL,
    protein_g       REAL,
    fat_g           REAL,
    carb_g          REAL,
    salt_g          REAL,
    quantity_g      REAL,
    source_type     TEXT NOT NULL,
    confidence      TEXT NOT NULL,
    linked_image_url TEXT,
    note            TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, date, meal_slot, food_name)
);

CREATE INDEX IF NOT EXISTS idx_user_date ON entries(user_id, date);

CREATE TABLE IF NOT EXISTS activity (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    date            TEXT NOT NULL,
    total_kcal      REAL NOT NULL,
    active_kcal     REAL,
    resting_kcal    REAL,
    source_type     TEXT NOT NULL,
    ocr_image_url   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, date)
);

CREATE TABLE IF NOT EXISTS weight_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL,
    date            TEXT NOT NULL,
    weight_kg       REAL NOT NULL,
    is_measured     INTEGER NOT NULL,
    body_fat_pct    REAL,
    muscle_kg       REAL,
    bmr_kcal        REAL,
    note            TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, date)
);
