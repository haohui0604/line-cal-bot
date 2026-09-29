from pydantic_settings import BaseSettings
from pathlib import Path
import os

class Settings(BaseSettings):
    LINE_CHANNEL_ACCESS_TOKEN: str = ""
    LINE_CHANNEL_SECRET: str = ""
    OCR_BACKEND: str = "gemini"
    GEMINI_API_KEY: str = ""
    DB_PATH: str = "./data/cal.db"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    TURSO_DATABASE_URL: str = os.getenv("TURSO_DATABASE_URL", "")
    TURSO_AUTH_TOKEN: str = os.getenv("TURSO_AUTH_TOKEN", "")

    # --- Web / LINEログイン (Phase 1) ---
    LINE_LOGIN_CHANNEL_ID: str = ""
    LINE_LOGIN_CHANNEL_SECRET: str = ""
    BASE_URL: str = "http://localhost:8000"  # 本番: https://your-app.onrender.com
    SESSION_SECRET: str = "dev-only-secret"  # 本番は必ず長い乱数に変更

    class Config:
        env_file = ".env"

settings = Settings()

Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
