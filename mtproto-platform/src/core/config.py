from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    env: str = "dev"
    log_level: str = "INFO"
    # تنظیمات پیش‌فرض برای اجرای لوکال
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/mtproto"

    model_config = SettingsConfigDict(
        env_file=".env", 
        env_file_encoding="utf-8", 
        extra="ignore"
    )


def get_settings() -> Settings:
    return Settings()