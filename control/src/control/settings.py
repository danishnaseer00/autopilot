"""Process settings. Values are loaded from the environment (paas/.env in dev)
and are never printed: repr=False hides every field from accidental logging.

One DSN per role (SPEC-03 R-2):
  app_database_url    -> autopilot_app;  RLS applies, this is request traffic
  audit_database_url  -> autopilot_audit; audit_log inserts only
The migrator DSN (DATABASE_URL) is NOT needed by the app process and is
deliberately absent: the app never holds a BYPASSRLS credential.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        repr=False,
    )

    env_name: str = "dev"
    app_database_url: str | None = None
    audit_database_url: str | None = None
    redis_url: str | None = None
    secret_key_base: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
