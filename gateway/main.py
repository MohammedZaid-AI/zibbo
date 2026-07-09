"""Application factory and process entrypoint.

``create_app`` accepts an explicit ``Settings`` so tests can build an isolated
application without touching the environment or the settings cache.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio.to_thread
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.api.router import api_router
from gateway.config import Settings, get_settings
from gateway.errors import register_exception_handlers
from gateway.health import HealthRegistry
from gateway.logging import configure_logging, get_logger
from gateway.middleware.request_context import (
    GATEWAY_REQUEST_ID_HEADER,
    OPTIMIZATION_HEADER,
    PROCESS_TIME_HEADER,
    REQUEST_ID_HEADER,
    TOKENS_SAVED_HEADER,
    RequestContextMiddleware,
)
from gateway.optimizers import build_pipeline
from gateway.providers import OpenAIProvider, ProviderRegistry, ProxyService
from gateway.tokenizers import TokenCounterFactory

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


def build_upstream_client(settings: Settings) -> httpx.AsyncClient:
    """One connection pool for the whole process.

    A client per request would open a new TCP+TLS connection to the provider every
    time, adding a full handshake to every call. Redirects are not followed: a
    transparent proxy hands the 3xx to the caller and lets the SDK decide.
    """
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_seconds,
            read=settings.upstream_read_timeout_seconds,
            write=settings.upstream_write_timeout_seconds,
            pool=settings.upstream_pool_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=settings.upstream_max_connections,
            max_keepalive_connections=settings.upstream_max_keepalive_connections,
        ),
        follow_redirects=False,
    )


def build_provider_registry(settings: Settings) -> ProviderRegistry:
    """Register every configured provider. Phase 6 adds Anthropic here."""
    registry = ProviderRegistry()
    registry.register(
        OpenAIProvider(base_url=settings.openai_base_url, api_key=settings.openai_api_key)
    )
    return registry


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the lifecycle of every long-lived resource the app holds."""
    settings: Settings = app.state.settings

    app.state.started_at = time.monotonic()
    app.state.health = HealthRegistry(timeout_seconds=settings.health_check_timeout_seconds)
    app.state.providers = build_provider_registry(settings)
    app.state.upstream_client = build_upstream_client(settings)
    app.state.proxy = ProxyService(app.state.upstream_client)

    app.state.token_counters = TokenCounterFactory.from_settings(settings)
    app.state.pipeline = build_pipeline(settings, app.state.token_counters)

    # tiktoken fetches its encoding over the network on first use. Do it now, in a
    # worker thread, so the cost never lands inside a user's request. A failure is
    # not fatal: the factory falls back to approximate counting.
    exact_tokens = await anyio.to_thread.run_sync(app.state.token_counters.prewarm)

    # Phase 4 registers the Postgres probe here; Phase 8 registers Redis.
    # Provider reachability is deliberately not probed: it would bill the
    # account and burn rate limit on every readiness check.

    logger.info(
        "application_started",
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
        docs_enabled=settings.docs_enabled,
        probes=app.state.health.names,
        providers=app.state.providers.names,
        optimization_enabled=settings.optimization_enabled,
        exact_token_counting=exact_tokens,
    )

    try:
        yield
    finally:
        await app.state.upstream_client.aclose()
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
            expose_headers=[
                REQUEST_ID_HEADER,
                GATEWAY_REQUEST_ID_HEADER,
                PROCESS_TIME_HEADER,
                OPTIMIZATION_HEADER,
                TOKENS_SAVED_HEADER,
            ],
        )
    app.add_middleware(RequestContextMiddleware, quiet_paths=QUIET_PATHS)

    register_exception_handlers(app)
    app.include_router(api_router)

    return app


app = create_app()
