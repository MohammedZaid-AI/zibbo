"""FastAPI dependency providers.

Everything a route needs is reached through one of these, so tests can override
a single symbol instead of monkeypatching module globals.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from gateway.config import Settings, get_settings
from gateway.health import HealthRegistry
from gateway.optimizers import TransformationPipeline
from gateway.plugins import PluginManager
from gateway.providers import ProviderRegistry, ProxyService


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


def get_start_time(request: Request) -> float:
    """Monotonic timestamp captured when the application started."""
    started: float = request.app.state.started_at
    return started


SettingsDep = Annotated[Settings, Depends(get_settings)]
HealthRegistryDep = Annotated[HealthRegistry, Depends(get_health_registry)]
StartTimeDep = Annotated[float, Depends(get_start_time)]
ProviderRegistryDep = Annotated[ProviderRegistry, Depends(get_provider_registry)]
ProxyServiceDep = Annotated[ProxyService, Depends(get_proxy_service)]
PipelineDep = Annotated[TransformationPipeline, Depends(get_pipeline)]
PluginManagerDep = Annotated[PluginManager, Depends(get_plugin_manager)]
