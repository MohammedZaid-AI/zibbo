"""Typed application configuration, sourced exclusively from the environment.

Settings are resolved once per process and injected via :func:`get_settings`, which
doubles as a FastAPI dependency. Tests override the cache rather than mutating a
global, so no module ever reads ``os.environ`` directly.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Any, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from gateway import __version__

ENV_PREFIX = "LLMGATEWAY_"


class Environment(StrEnum):
    """Deployment environment. Controls defaults that must not leak into production."""

    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class LogFormat(StrEnum):
    """Rendering style for log records."""

    JSON = "json"
    CONSOLE = "console"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Settings(BaseSettings):
    """Runtime configuration for the gateway process."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # -- Identity ----------------------------------------------------------
    app_name: str = "llmgateway"
    app_version: str = __version__
    environment: Environment = Environment.DEVELOPMENT

    # -- Server ------------------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 — bind-all is intended inside a container
    port: Annotated[int, Field(ge=1, le=65535)] = 8000
    root_path: str = ""
    """Mount prefix when running behind a path-rewriting proxy."""

    # -- Observability -----------------------------------------------------
    debug: bool = False
    log_level: LogLevel = LogLevel.INFO
    log_format: LogFormat = LogFormat.CONSOLE

    # -- HTTP --------------------------------------------------------------
    # NoDecode suppresses pydantic-settings' automatic json.loads of list fields,
    # which would otherwise reject the comma-separated form before the validator
    # below ever runs.
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # -- Upstream providers ------------------------------------------------
    upstream_connect_timeout_seconds: Annotated[float, Field(gt=0)] = 10.0
    upstream_read_timeout_seconds: Annotated[float, Field(gt=0)] = 120.0

    # -- Backing services (wired up in later phases) -----------------------
    database_url: str | None = None
    redis_url: str | None = None

    # -- Health ------------------------------------------------------------
    health_check_timeout_seconds: Annotated[float, Field(gt=0)] = 2.0

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def _parse_origins(cls, value: Any) -> Any:
        """Accept ``a,b``, ``["a","b"]``, or an already-parsed list.

        Env vars are strings; a JSON array is the documented form but a bare
        comma-separated list is what people actually type into a compose file.
        """
        if not isinstance(value, str):
            return value

        stripped = value.strip()
        if not stripped:
            return []
        if stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"cors_allow_origins is not valid JSON: {exc}") from exc
        return [item.strip() for item in stripped.split(",") if item.strip()]

    @field_validator("root_path")
    @classmethod
    def _normalize_root_path(cls, value: str) -> str:
        value = value.rstrip("/")
        if value and not value.startswith("/"):
            value = f"/{value}"
        return value

    @model_validator(mode="after")
    def _guard_production(self) -> Self:
        """Refuse configurations that are unsafe once real traffic is on the box."""
        if self.environment.is_production_like:
            if self.debug:
                raise ValueError("debug must be disabled in staging/production")
            if "*" in self.cors_allow_origins:
                raise ValueError("wildcard CORS origin is not permitted in staging/production")
        return self

    @property
    def docs_enabled(self) -> bool:
        """OpenAPI docs are served everywhere except production."""
        return self.environment is not Environment.PRODUCTION


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that FastAPI's dependency system, middleware, and the logging
    bootstrap all observe the same object. Call ``get_settings.cache_clear()``
    in tests after changing the environment.
    """
    return Settings()
