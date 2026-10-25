"""Process settings. Values are loaded from the environment (paas/.env in dev)
and are never printed: repr=False hides every field from accidental logging.

One DSN per role (SPEC-03 R-2):
  app_database_url    -> autopilot_app;  RLS applies, this is request traffic
  audit_database_url  -> autopilot_audit; audit_log inserts only
The migrator DSN (DATABASE_URL) is NOT needed by the app process and is
deliberately absent: the app never holds a BYPASSRLS credential.

`dev_login_enabled` is a local-only bootstrap. It is false by default, refused
unless `env_name == "dev"`, and must stay false in every deployment. Production
uses the GitHub OAuth path.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        repr=False,
    )

    # Safe default: local development opts into "dev" explicitly in .env. Both
    # names are accepted because the deployment manifests use APP_ENV_NAME
    # while the field itself is env_name.
    env_name: str = Field(
        default="production",
        validation_alias=AliasChoices("APP_ENV_NAME", "ENV_NAME"),
    )
    app_database_url: str | None = None
    audit_database_url: str | None = None
    redis_url: str | None = None
    secret_key_base: str = ""

    # Sessions (SPEC-05 R-1). The cookie is opaque; Secure is on by default and
    # turned off only by the local HTTP profile.
    session_cookie_name: str = "autopilot_session"
    session_ttl_hours: int = 24
    session_idle_hours: int = 8
    cookie_secure: bool = True

    # Local developer login. Refused outside env_name=dev even if this is on.
    dev_login_enabled: bool = False
    dev_login_email: str = "dev@autopilot.local"
    dev_login_org: str = "dev"

    # SPEC-18 master key. Local: a 32-byte file outside git. Production: a
    # mounted KMS/file secret; there is deliberately no plain env-var key.
    master_key_file: str | None = None

    # GitHub OAuth (SPEC-05). Empty values disable the routes with a 503.
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = "http://localhost:8000/api/v1/auth/github/callback"
    console_base_url: str = "http://localhost:5173"

    # Public routing base used when the API needs to tell a client where an app
    # will live. Local: *.apps.localtest.me; AWS: the configured wildcard zone.
    apps_domain: str = "apps.localtest.me"

    # Digest-pinned builder recorded on a deployment before detection runs.
    # The build worker replaces it with the strategy-specific catalogue entry
    # when it claims the job; this value must still be a real digest so the
    # database cannot store a mutable tag (SPEC-09 R-23). Deployments override
    # it with the pinned builder for their build strategy.
    default_build_image: str = (
        "busybox@sha256:"
        "73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
