CREATE TABLE IF NOT EXISTS user_settings (
  user_id      TEXT PRIMARY KEY,
  bot_name     TEXT NOT NULL DEFAULT 'アシスタント',
  bot_tone     TEXT NOT NULL DEFAULT '',
  bot_pronoun  TEXT NOT NULL DEFAULT 'わたし',
  updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
