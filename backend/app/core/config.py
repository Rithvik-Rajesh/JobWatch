from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration from environment / .env. Secrets never live in the database."""

    model_config = SettingsConfigDict(env_prefix="JOBWATCH_", env_file=".env", extra="ignore")

    env: str = Field(default="development", pattern="^(development|test|production)$")
    database_url: str = "postgresql+asyncpg://jobwatch:jobwatch@127.0.0.1:5433/jobwatch"

    # Single-user auth. Generate the hash with:  uv run python -m app.core.auth hash-password
    admin_username: str = "admin"
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("dev-insecure-session-secret-change-me")
    session_max_age_days: int = 30
    # Bearer token for the "Save to JobWatch" bookmarklet (cross-origin capture endpoint).
    capture_token: SecretStr = SecretStr("")

    cors_origins: list[str] = ["http://localhost:5173"]
    log_level: str = "INFO"
    log_json: bool = False

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
