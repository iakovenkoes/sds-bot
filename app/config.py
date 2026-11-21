"""Private settings come from environment variables, not from source code."""
from pydantic import SecretStr, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')
    bot_token: SecretStr
    database_url: str = 'postgresql+asyncpg://atlantis:atlantis@localhost:5432/atlantis'
    super_admin_id: int = Field(gt=0)
    support_chat_id: int = 0
    review_chat_id: int = 0
    bot_username: str = 'atlantis_sha26_bot'
    test_mode: bool = False  # Local-only preview, for SUPER_ADMIN_ID. Disable before public launch.


def load_settings() -> Settings:
    return Settings()
