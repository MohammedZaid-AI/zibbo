"""Application factory and process entrypoint.

``create_app`` accepts an explicit ``Settings`` so tests can build an isolated
application without touching the environment or the settings cache.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.api.router import api_router
from gateway.config import Settings, get_settings
from gateway.errors import register_exception_handlers
from gateway.health import HealthRegistry
from gateway.logging import configure_logging, get_logger
from gateway.middleware.request_context import (
    PROCESS_TIME_HEADER,
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = get_logger(__name__)

QUIET_PATHS = frozenset({"/health/live", "/health/ready"})

DESCRIPTION = """\
LLMGateway is a drop-in proxy for LLM providers. Point your SDK's `base_url` at
this service and requests are deterministically optimized — structural noise
stripped, content normalized to Markdown — before being forwarded upstream.

No LLM is used to optimize. No content is summarized. Meaning is preserved.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the lifecycle of every long-lived resource the app holds."""
    settings: Settings = app.state.settings

    app.state.started_at = time.monotonic()
    app.state.health = HealthRegistry(timeout_seconds=settings.health_check_timeout_seconds)

    # Phase 4 registers the Postgres probe here; Phase 8 registers Redis.

    logger.info(
        "application_started",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
        docs_enabled=settings.docs_enabled,
        probes=app.state.health.names,
    )

    try:
        yield
    finally:
        logger.info("application_stopped", service=settings.app_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a fully wired application instance."""
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="LLMGateway",
        description=DESCRIPTION,
        version=settings.app_version,
        root_path=settings.root_path,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings

    # Middleware added last is outermost. RequestContextMiddleware must wrap CORS
    # so that preflight responses also carry a request id.
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allow_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[REQUEST_ID_HEADER, PROCESS_TIME_HEADER],
        )
    app.add_middleware(RequestContextMiddleware, quiet_paths=QUIET_PATHS)

    register_exception_handlers(app)
    app.include_router(api_router)

    return app


app = create_app()
