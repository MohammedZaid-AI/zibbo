"""Typed application configuration, sourced exclusively from the environment.

Settings are resolved once per process and injected via :func:`get_settings`, which
doubles as a FastAPI dependency. Tests override the cache rather than mutating a
global, so no module ever reads ``os.environ`` directly.
"""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import Field, SecretStr, field_validator, model_validator
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


class TokenizerBackend(StrEnum):
    """Which token counter to use.

    ``AUTO`` prefers tiktoken and silently falls back to the heuristic counter if
    its encoding files cannot be loaded — they are fetched over the network on
    first use, which must never fail a request.
    """

    AUTO = "auto"
    TIKTOKEN = "tiktoken"
    HEURISTIC = "heuristic"


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

    # -- Upstream transport ------------------------------------------------
    upstream_connect_timeout_seconds: Annotated[float, Field(gt=0)] = 10.0
    upstream_read_timeout_seconds: Annotated[float, Field(gt=0)] = 120.0
    upstream_write_timeout_seconds: Annotated[float, Field(gt=0)] = 30.0
    upstream_pool_timeout_seconds: Annotated[float, Field(gt=0)] = 10.0
    upstream_max_connections: Annotated[int, Field(gt=0)] = 200
    upstream_max_keepalive_connections: Annotated[int, Field(gt=0)] = 50

    # -- Providers ---------------------------------------------------------
    # Each provider mounts at its own route prefix. A caller selects a provider by
    # the URL its SDK points at; a fallback credential is used only when the caller
    # sends none, so an existing app switches over by changing base_url alone.

    openai_enabled: bool = True
    openai_prefix: str = "/v1"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: SecretStr | None = None

    anthropic_enabled: bool = True
    # The Anthropic SDK appends `/v1/messages` to its base URL, whereas the OpenAI
    # SDK carries `/v1` in the base URL and appends `/chat/completions`. So a caller
    # points the Anthropic SDK at `<gateway>/anthropic`, and the gateway forwards the
    # `/v1/...` the SDK adds onto the provider origin. Mirroring each SDK's own URL
    # construction is what keeps the drop-in promise for both.
    anthropic_prefix: str = "/anthropic"
    anthropic_base_url: str = "https://api.anthropic.com"
    anthropic_api_key: SecretStr | None = None
    anthropic_version: str = "2023-06-01"

    # OpenAI-compatible providers. Disabled unless a base URL is set, since there is
    # no universal default. Groq and Mistral are cloud; Ollama is usually local.
    groq_prefix: str = "/groq/v1"
    groq_base_url: str | None = None
    groq_api_key: SecretStr | None = None

    mistral_prefix: str = "/mistral/v1"
    mistral_base_url: str | None = None
    mistral_api_key: SecretStr | None = None

    ollama_prefix: str = "/ollama/v1"
    ollama_base_url: str | None = None
    ollama_api_key: SecretStr | None = None

    # -- Backing services (wired up in later phases) -----------------------
    database_url: str | None = None
    redis_url: str | None = None

    # -- Optimization ------------------------------------------------------
    optimization_enabled: bool = True

    optimization_max_body_bytes: Annotated[int, Field(gt=0)] = 8_000_000
    """Bodies above this are forwarded untouched rather than parsed."""

    optimization_offload_threshold_bytes: Annotated[int, Field(gt=0)] = 131_072
    """Above this, transformation runs in a worker thread. Parsing a multi-megabyte
    HTML document takes tens of milliseconds and would otherwise stall the event
    loop for every other in-flight request."""

    optimization_min_segment_chars: Annotated[int, Field(ge=0)] = 0
    """Segments shorter than this are left alone; the overhead exceeds the saving."""

    # Off by default: an empty list can be semantically meaningful (`"tools": []`
    # means something different from an absent key), so removing it is opt-in.
    json_remove_empty_containers: bool = False

    # Off by default: collapsing runs of spaces destroys code indentation and
    # Markdown table alignment, which are meaning, not noise.
    text_collapse_inline_whitespace: bool = False
    text_dedupe_consecutive_paragraphs: bool = True

    html_preserve_links: bool = True
    html_preserve_images: bool = True

    # -- Tokenizer ---------------------------------------------------------
    tokenizer: TokenizerBackend = TokenizerBackend.AUTO
    tokenizer_default_encoding: str = "o200k_base"

    # -- Plugins -----------------------------------------------------------
    plugins_enabled: bool = True

    plugins_entry_point_group: str = "llmgateway.transformers"
    """Installed packages advertising a transformer in this entry-point group."""

    plugins_dir: Path | None = None
    """Load ``*.py`` files and packages from this directory. Executes arbitrary code
    from the filesystem, so there is deliberately no default."""

    plugins_load: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Explicit ``module`` or ``module:ATTR`` targets. Highest precedence."""

    plugins_disabled: Annotated[list[str], NoDecode] = Field(default_factory=list)
    """Loaded and validated, but not attached to the pipeline."""

    plugins_allow_lossy: bool = False
    """Permit plugins that declare the ``lossy`` capability."""

    plugins_config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """Per-plugin configuration, keyed by plugin name. JSON in the environment."""

    # -- Health ------------------------------------------------------------
    health_check_timeout_seconds: Annotated[float, Field(gt=0)] = 2.0

    @field_validator("cors_allow_origins", "plugins_load", "plugins_disabled", mode="before")
    @classmethod
    def _parse_string_list(cls, value: Any) -> Any:
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
                raise ValueError(f"value is not valid JSON: {exc}") from exc
        return [item.strip() for item in stripped.split(",") if item.strip()]

    @field_validator(
        "openai_base_url",
        "anthropic_base_url",
        "groq_base_url",
        "mistral_base_url",
        "ollama_base_url",
    )
    @classmethod
    def _strip_trailing_slash(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value is not None else None

    @field_validator(
        "root_path",
        "openai_prefix",
        "anthropic_prefix",
        "groq_prefix",
        "mistral_prefix",
        "ollama_prefix",
    )
    @classmethod
    def _normalize_prefix(cls, value: str) -> str:
        value = value.rstrip("/")
        if value and not value.startswith("/"):
            value = f"/{value}"
        return value

    @model_validator(mode="after")
    def _guard_route_prefixes(self) -> Self:
        """Two providers sharing a prefix would make routing ambiguous."""
        prefixes: dict[str, str] = {}
        for provider, enabled, prefix in self.enabled_provider_prefixes:
            if not enabled:
                continue
            if prefix in prefixes:
                raise ValueError(
                    f"providers {prefixes[prefix]!r} and {provider!r} both mount at {prefix!r}"
                )
            prefixes[prefix] = provider
        return self

    @property
    def enabled_provider_prefixes(self) -> tuple[tuple[str, bool, str], ...]:
        """``(provider_name, is_enabled, route_prefix)`` for every known provider.

        An OpenAI-compatible provider is enabled exactly when its base URL is set.
        """
        return (
            ("openai", self.openai_enabled, self.openai_prefix),
            ("anthropic", self.anthropic_enabled, self.anthropic_prefix),
            ("groq", self.groq_base_url is not None, self.groq_prefix),
            ("mistral", self.mistral_base_url is not None, self.mistral_prefix),
            ("ollama", self.ollama_base_url is not None, self.ollama_prefix),
        )

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
