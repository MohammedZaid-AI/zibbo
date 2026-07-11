"""Shared fixtures.

Every test builds its own application from an explicit ``Settings`` object, so
the environment and the ``get_settings`` cache never leak between tests.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from gateway.config import Environment, LogFormat, LogLevel, Settings, get_settings
from gateway.main import create_app


def build_settings(**overrides: object) -> Settings:
    """A Settings instance isolated from ``.env`` and from ``ZIBBO_*`` vars."""
    defaults: dict[str, object] = {
        "environment": Environment.TEST,
        "debug": False,
        "log_level": LogLevel.WARNING,
        "log_format": LogFormat.CONSOLE,
        "cors_allow_origins": [],
        "health_check_timeout_seconds": 1.0,
        # Off by default so the suite does not depend on which plugins happen to be
        # installed in the developer's environment. Plugin tests opt in explicitly.
        "plugins_enabled": False,
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return build_settings()


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    application = create_app(settings)
    # Route handlers resolve settings through the DI system, not the module cache.
    application.dependency_overrides[get_settings] = lambda: settings
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the ASGI app, with lifespan run."""
    # ASGITransport does not drive the lifespan protocol, so do it explicitly;
    # /health depends on state populated during startup.
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://gateway.test") as http_client:
            yield http_client


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
