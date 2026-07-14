"""Application factory and process entrypoint.

``create_app`` accepts an explicit ``Settings`` so tests can build an isolated
application without touching the environment or the settings cache.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio.to_thread
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from gateway.analytics import AnalyticsEngine
from gateway.api.router import api_router
from gateway.api.routes.proxy import create_proxy_router
from gateway.cache import build_transformation_cache
from gateway.config import Settings, get_settings
from gateway.documents import build_document_service
from gateway.errors import register_exception_handlers
from gateway.health import ComponentHealth, HealthRegistry, HealthStatus
from gateway.logging import configure_logging, get_logger
from gateway.middleware.request_context import (
    CACHE_HEADER,
    GATEWAY_REQUEST_ID_HEADER,
    OPTIMIZATION_HEADER,
    PROCESS_TIME_HEADER,
    REQUEST_ID_HEADER,
    TOKENS_SAVED_HEADER,
    RequestContextMiddleware,
)
from gateway.optimizers import (
    ContentDetector,
    OptimizerOptions,
    apply_prompt_optimization,
    build_pipeline,
    build_provider_policy,
    build_transformer_registry,
)
from gateway.plugins import PluginManager
from gateway.providers import (
    AnthropicProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderRegistry,
    ProxyService,
)
from gateway.runtime import RuntimeControl
from gateway.tokenizers import TokenCounterFactory

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from gateway.providers import Provider

logger = get_logger(__name__)

QUIET_PATHS = frozenset({"/health/live", "/health/ready"})

DESCRIPTION = """\
Zibbo is a drop-in proxy for LLM providers. Point your SDK's `base_url` at
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


@dataclass(frozen=True, slots=True)
class MountedProvider:
    provider: Provider
    prefix: str


def build_providers(settings: Settings) -> list[MountedProvider]:
    """Every enabled provider and the route prefix it mounts at.

    This is the *only* place provider classes are named. A provider absent from this
    list simply is not served; a provider added to it needs no other change. The
    OpenAI-compatible providers share one class, distinguished by name and base URL.
    """
    mounted: list[MountedProvider] = []

    if settings.openai_enabled:
        mounted.append(
            MountedProvider(
                OpenAIProvider(base_url=settings.openai_base_url, api_key=settings.openai_api_key),
                settings.openai_prefix,
            )
        )
    if settings.anthropic_enabled:
        mounted.append(
            MountedProvider(
                AnthropicProvider(
                    base_url=settings.anthropic_base_url,
                    api_key=settings.anthropic_api_key,
                    version=settings.anthropic_version,
                ),
                settings.anthropic_prefix,
            )
        )
    for name, base_url, api_key, prefix in (
        ("groq", settings.groq_base_url, settings.groq_api_key, settings.groq_prefix),
        ("mistral", settings.mistral_base_url, settings.mistral_api_key, settings.mistral_prefix),
        ("ollama", settings.ollama_base_url, settings.ollama_api_key, settings.ollama_prefix),
    ):
        if base_url is not None:
            mounted.append(
                MountedProvider(
                    OpenAICompatibleProvider(name=name, base_url=base_url, api_key=api_key),
                    prefix,
                )
            )

    return mounted


def build_provider_registry(mounted: list[MountedProvider]) -> ProviderRegistry:
    registry = ProviderRegistry()
    for entry in mounted:
        registry.register(entry.provider)
    return registry


def _make_cache_probe(cache: object) -> Callable[[], Awaitable[ComponentHealth]]:
    """A readiness probe for the Redis cache backend.

    Reports ``DEGRADED`` (not ``UNHEALTHY``) when Redis is unreachable: the gateway
    still serves every request, it just recomputes instead of reusing. The ping runs in
    a worker thread because the backend is synchronous.
    """
    from gateway.cache import TransformationCache

    assert isinstance(cache, TransformationCache)  # noqa: S101 — internal wiring invariant

    async def probe() -> ComponentHealth:
        reachable = await anyio.to_thread.run_sync(cache.probe)
        status = HealthStatus.OK if reachable else HealthStatus.DEGRADED
        detail = None if reachable else "cache backend unreachable; serving without cache"
        return ComponentHealth(name="cache", status=status, detail=detail)

    return probe


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the lifecycle of every long-lived resource the app holds."""
    settings: Settings = app.state.settings

    app.state.started_at = time.monotonic()
    app.state.health = HealthRegistry(timeout_seconds=settings.health_check_timeout_seconds)
    app.state.upstream_client = build_upstream_client(settings)
    app.state.proxy = ProxyService(app.state.upstream_client)

    app.state.token_counters = TokenCounterFactory.from_settings(settings)

    # Plugins attach to the registry and detector *before* the pipeline is built.
    # `load()` and `attach()` never raise: a third-party package must not be able to
    # stop this gateway from starting.
    options = OptimizerOptions.from_settings(settings)
    registry = build_transformer_registry(options)
    detector = ContentDetector()

    plugins = PluginManager.from_settings(settings)
    plugins.load()
    plugin_report = plugins.attach(registry, detector)
    app.state.plugins = plugins

    # Prompt de-duplication: seed the detector's sniffer to match the runtime flag
    # (itself seeded from settings). The transformer was already registered by
    # build_transformer_registry when enabled; this brings the sniffer into line, and is
    # also the entry point the /internal enable/disable endpoints reuse live.
    apply_prompt_optimization(
        registry, detector, options, enabled=app.state.runtime.prompt_optimization_enabled
    )

    # The internal API's /status lists the active transformers; expose the registry and
    # detector it reads from and toggles. Kept on state rather than reached through the
    # pipeline so the two do not have to grow a coupling for one read.
    app.state.transformer_registry = registry
    app.state.detector = detector
    app.state.optimizer_options = options

    document_service = build_document_service(settings)
    app.state.documents = document_service

    # The transformation cache: identical content is extracted/normalized once and
    # reused. Always constructed; a no-op when disabled. Its backend failing (Redis
    # down) degrades to a cache miss, never a request failure.
    cache = build_transformation_cache(settings)
    app.state.cache = cache

    app.state.pipeline = build_pipeline(
        settings,
        app.state.token_counters,
        registry=registry,
        detector=detector,
        document_service=document_service,
        cache=cache,
    )

    # A Redis cache is a real dependency, so it gets a readiness probe — but a
    # *degraded* one: an unreachable cache slows the gateway, it does not stop it
    # serving, so it must not fail readiness and pull the pod from the balancer.
    if settings.cache_enabled and cache.backend_name == "redis":
        app.state.health.register("cache", _make_cache_probe(cache))

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
        providers={entry.provider.name: entry.prefix for entry in app.state.mounted_providers},
        optimization_enabled=settings.optimization_enabled,
        exact_token_counting=exact_tokens,
        transformers=registry.names,
        documents_enabled=document_service.enabled,
        cache_enabled=cache.enabled,
        cache_backend=cache.backend_name,
        plugins_enabled=plugin_report.enabled,
        plugins_failed=tuple(record.name for record in plugin_report.failed),
    )

    try:
        yield
    finally:
        await app.state.upstream_client.aclose()
        cache.close()
        logger.info("application_stopped", service=settings.app_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a fully wired application instance."""
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Zibbo",
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

    # Live runtime switches and the in-memory analytics engine. Created here, not in
    # lifespan, because the per-provider policies built below close over the runtime
    # control to read the optimization kill switch live. The analytics engine records
    # every request's optimization outcome for the plugin's /internal/stats view.
    app.state.runtime = RuntimeControl(
        optimization_enabled=settings.optimization_enabled,
        prompt_optimization_enabled=settings.prompt_optimization_enabled,
    )
    app.state.analytics = AnalyticsEngine()

    # Providers are constructed here — synchronously, no I/O — so their route
    # prefixes are known before the app serves. The registry and per-provider
    # policies are read at request time; the pipeline they use is built in lifespan.
    mounted = build_providers(settings)
    app.state.mounted_providers = mounted
    app.state.providers = build_provider_registry(mounted)

    # Per-provider optimization policy. The pipeline is shared and provider-agnostic;
    # what differs by provider — its eligible endpoints — is bound here and handed to
    # the pipeline per request. Adapters travel on the provider itself. The runtime
    # control makes the enable/disable switch take effect without a restart.
    app.state.provider_policies = {
        entry.provider.name: build_provider_policy(
            settings, entry.provider.endpoint_policy, app.state.runtime
        )
        for entry in mounted
    }

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
                CACHE_HEADER,
            ],
        )
    app.add_middleware(RequestContextMiddleware, quiet_paths=QUIET_PATHS)

    register_exception_handlers(app)
    app.include_router(api_router)

    # One proxy router per provider, at its configured prefix. The route knows its
    # provider by name; it holds no provider logic of its own.
    for entry in mounted:
        app.include_router(
            create_proxy_router(provider_name=entry.provider.name, prefix=entry.prefix)
        )

    return app


app = create_app()
