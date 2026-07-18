"""Configuration parsing and the production safety rails."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gateway.config import Environment, Settings, get_settings
from tests.conftest import build_settings


def test_defaults_are_development_safe() -> None:
    settings = Settings(_env_file=None)
    assert settings.environment is Environment.DEVELOPMENT
    assert settings.port == 8000
    assert settings.docs_enabled is True


def test_docs_are_disabled_in_production() -> None:
    settings = build_settings(environment=Environment.PRODUCTION)
    assert settings.docs_enabled is False


def test_docs_remain_enabled_in_staging() -> None:
    assert build_settings(environment=Environment.STAGING).docs_enabled is True


def test_debug_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="debug must be disabled"):
        build_settings(environment=Environment.PRODUCTION, debug=True)


def test_wildcard_cors_is_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="wildcard CORS origin"):
        build_settings(environment=Environment.PRODUCTION, cors_allow_origins=["*"])


def test_wildcard_cors_is_allowed_in_development() -> None:
    settings = build_settings(environment=Environment.DEVELOPMENT, cors_allow_origins=["*"])
    assert settings.cors_allow_origins == ["*"]


_ORIGIN_FORMS = [
    ("http://a.test,http://b.test", ["http://a.test", "http://b.test"]),
    ("http://a.test , http://b.test ", ["http://a.test", "http://b.test"]),
    ("http://a.test", ["http://a.test"]),
    ("", []),
    ('["http://a.test"]', ["http://a.test"]),
    ('["http://a.test", "http://b.test"]', ["http://a.test", "http://b.test"]),
]


@pytest.mark.parametrize(("raw", "expected"), _ORIGIN_FORMS)
def test_cors_origins_accept_csv_and_json(raw: str, expected: list[str]) -> None:
    assert build_settings(cors_allow_origins=raw).cors_allow_origins == expected


@pytest.mark.parametrize(("raw", "expected"), _ORIGIN_FORMS)
def test_cors_origins_parse_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    """The env var is the only path that matters in production.

    Without `NoDecode`, pydantic-settings json.loads() list fields before any
    validator runs, so the comma-separated form — which is what .env.example and
    docker-compose.yml both use — raised SettingsError at startup.
    """
    monkeypatch.setenv("ZIBBO_CORS_ALLOW_ORIGINS", raw)
    get_settings.cache_clear()

    assert get_settings().cors_allow_origins == expected


def test_malformed_json_origins_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIBBO_CORS_ALLOW_ORIGINS", '["unterminated')
    get_settings.cache_clear()

    with pytest.raises(ValidationError, match="not valid JSON"):
        get_settings()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", ""), ("/gw", "/gw"), ("gw", "/gw"), ("/gw/", "/gw")],
)
def test_root_path_is_normalized(raw: str, expected: str) -> None:
    assert build_settings(root_path=raw).root_path == expected


def test_settings_are_frozen() -> None:
    settings = build_settings()
    with pytest.raises(ValidationError):
        settings.port = 9999  # type: ignore[misc]


def test_environment_is_read_from_prefixed_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZIBBO_PORT", "9101")
    monkeypatch.setenv("ZIBBO_ENVIRONMENT", "staging")
    get_settings.cache_clear()

    settings = get_settings()
    assert settings.port == 9101
    assert settings.environment is Environment.STAGING


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()


@pytest.mark.parametrize("port", [0, 70000, -1])
def test_port_must_be_in_range(port: int) -> None:
    with pytest.raises(ValidationError):
        build_settings(port=port)
