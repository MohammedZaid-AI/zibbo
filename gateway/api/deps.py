"""FastAPI dependency providers.

Everything a route needs is reached through one of these, so tests can override
a single symbol instead of monkeypatching module globals.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from gateway.analytics import AnalyticsEngine
from gateway.cache import TransformationCache
from gateway.config import Settings, get_settings
from gateway.errors import ErrorType, GatewayError, NotFoundError
from gateway.health import HealthRegistry
from gateway.optimizers import TransformationPipeline
from gateway.plugins import PluginManager
from gateway.providers import ProviderRegistry, ProxyService
from gateway.runtime import RuntimeControl

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def get_health_registry(request: Request) -> HealthRegistry:
    """The registry created during application startup."""
    registry: HealthRegistry = request.app.state.health
    return registry


def get_provider_registry(request: Request) -> ProviderRegistry:
    """Providers configured during application startup."""
    registry: ProviderRegistry = request.app.state.providers
    return registry


def get_proxy_service(request: Request) -> ProxyService:
    """The proxy, bound to the process-wide upstream connection pool."""
    proxy: ProxyService = request.app.state.proxy
    return proxy


def get_pipeline(request: Request) -> TransformationPipeline:
    """The transformation pipeline assembled during startup."""
    pipeline: TransformationPipeline = request.app.state.pipeline
    return pipeline


def get_plugin_manager(request: Request) -> PluginManager:
    """Plugins discovered and attached during startup."""
    plugins: PluginManager = request.app.state.plugins
    return plugins


def get_cache(request: Request) -> TransformationCache:
    """The transformation cache assembled during startup."""
    cache: TransformationCache = request.app.state.cache
    return cache


def get_start_time(request: Request) -> float:
    """Monotonic timestamp captured when the application started."""
    started: float = request.app.state.started_at
    return started


def get_analytics(request: Request) -> AnalyticsEngine:
    """The in-memory analytics engine created during startup."""
    analytics: AnalyticsEngine = request.app.state.analytics
    return analytics


def get_runtime_control(request: Request) -> RuntimeControl:
    """The live runtime switches created during startup."""
    control: RuntimeControl = request.app.state.runtime
    return control


def require_local(request: Request, settings: SettingsDep) -> None:
    """Gate the /internal/* API to the loopback interface.

    The internal API describes and controls the deployment; it is for the local plugin,
    not for callers. By default a request from anywhere but loopback gets a 404 — not a
    403 — so the endpoints do not even advertise their existence off-box. Turning on
    ``internal_api_allow_remote`` opens them, but then a bearer token is mandatory.
    """
    client_host = request.client.host if request.client else None

    if not settings.internal_api_allow_remote:
        if client_host not in _LOOPBACK_HOSTS:
            raise NotFoundError("Not Found", code="not_found")
        return

    # Remote access is enabled: a token is required, and must match.
    expected = settings.internal_api_token
    if expected is None:
        raise GatewayError(
            "internal API remote access is enabled but no token is configured",
            status_code=503,
            error_type=ErrorType.SERVICE_UNAVAILABLE,
            code="internal_api_misconfigured",
        )
    presented = _bearer_token(request)
    if presented is None or presented != expected.get_secret_value():
        raise GatewayError(
            "invalid or missing internal API token",
            status_code=401,
            error_type=ErrorType.AUTHENTICATION,
            code="internal_api_unauthorized",
        )


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-zibbo-token")


SettingsDep = Annotated[Settings, Depends(get_settings)]
HealthRegistryDep = Annotated[HealthRegistry, Depends(get_health_registry)]
StartTimeDep = Annotated[float, Depends(get_start_time)]
ProviderRegistryDep = Annotated[ProviderRegistry, Depends(get_provider_registry)]
ProxyServiceDep = Annotated[ProxyService, Depends(get_proxy_service)]
PipelineDep = Annotated[TransformationPipeline, Depends(get_pipeline)]
PluginManagerDep = Annotated[PluginManager, Depends(get_plugin_manager)]
CacheDep = Annotated[TransformationCache, Depends(get_cache)]
AnalyticsDep = Annotated[AnalyticsEngine, Depends(get_analytics)]
RuntimeControlDep = Annotated[RuntimeControl, Depends(get_runtime_control)]
RequireLocalDep = Annotated[None, Depends(require_local)]
