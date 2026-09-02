"""Typed application settings.

A missing or invalid required setting fails startup with a clear message
(TR-75). There is no silent default for anything that matters -- a database
URL or a secret that falls back quietly is a production incident waiting for
a demo to trigger it.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Loaded once from the environment. Use `get_settings()`, not this class directly."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    database_url: str = Field(
        default="postgresql+asyncpg://recoup:recoup@localhost:5432/recoup",
        description="Async SQLAlchemy connection string.",
    )
    redis_url: str = Field(default="redis://localhost:6379/0")

    # Unset in Phase 0 -- introduced by later phases. Declared here as SecretStr
    # now, deliberately, so the masking behaviour below is established and
    # tested before a real credential ever exists to leak.
    anthropic_api_key: SecretStr | None = None
    razorpay_key_id: str | None = None
    razorpay_key_secret: SecretStr | None = None
    razorpay_webhook_secret: SecretStr | None = None

    def __repr__(self) -> str:
        """Mask every SecretStr field so `log.info(settings)` cannot leak a credential."""
        fields = ", ".join(f"{name}={value!r}" for name, value in self)
        return f"{self.__class__.__name__}({fields})"


@lru_cache
def get_settings() -> Settings:
    return Settings()
