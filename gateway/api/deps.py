"""FastAPI dependency providers.

Everything a route needs is reached through one of these, so tests can override
a single symbol instead of monkeypatching module globals.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from gateway.config import Settings, get_settings
from gateway.health import HealthRegistry


def get_health_registry(request: Request) -> HealthRegistry:
    """The registry created during application startup."""
    registry: HealthRegistry = request.app.state.health
    return registry


def get_start_time(request: Request) -> float:
    """Monotonic timestamp captured when the application started."""
    started: float = request.app.state.started_at
    return started


SettingsDep = Annotated[Settings, Depends(get_settings)]
HealthRegistryDep = Annotated[HealthRegistry, Depends(get_health_registry)]
StartTimeDep = Annotated[float, Depends(get_start_time)]
