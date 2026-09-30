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
    LIFF_ID: str = ""   # LIFFアプリのID（例: 2008123456-abcdefgh）
    BASE_URL: str = "http://localhost:8000"  # 本番: https://your-app.onrender.com
    SESSION_SECRET: str = "dev-only-secret"  # 本番は必ず長い乱数に変更
    # ジム作成などの管理操作を許可する LINE user ID（カンマ区切り）
    ADMIN_USER_IDS: str = ""
    # トレーナー/ジム管理者として登録済みのユーザーにもジム作成を許可する
    ALLOW_STAFF_GYM_CREATE: bool = True

    # --- スリープ対策（Render無料枠の15分スリープより短くする） ---
    SELF_PING_ENABLED: bool = True
    SELF_PING_INTERVAL_SEC: int = 600   # 10分

    @property
    def admin_user_id_set(self) -> set:
        return {s.strip() for s in self.ADMIN_USER_IDS.split(",") if s.strip()}

    class Config:
        env_file = ".env"

settings = Settings()

Path(settings.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
