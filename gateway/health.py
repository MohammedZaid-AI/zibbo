"""Pluggable health-check registry.

A component (Postgres in Phase 4, Redis in Phase 8, provider reachability later)
registers an async probe under a name. The readiness endpoint fans out over the
registry, so adding a dependency never means editing the endpoint.

Probes are individually timed and individually bounded: a hung Redis cannot make
the readiness check hang, it can only mark itself unhealthy.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

from gateway.logging import get_logger

logger = get_logger(__name__)


class HealthStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    """Outcome of a single probe."""

    name: str
    status: HealthStatus
    detail: str | None = None
    latency_ms: float | None = None

    @property
    def blocks_readiness(self) -> bool:
        """Degraded components still serve traffic; unhealthy ones must not."""
        return self.status is HealthStatus.UNHEALTHY


HealthProbe = Callable[[], Awaitable[ComponentHealth]]
"""A probe reports its own status and never raises. If it raises, we catch it."""


class HealthRegistry:
    """Holds the probes for a single application instance."""

    def __init__(self, *, timeout_seconds: float = 2.0) -> None:
        self._timeout_seconds = timeout_seconds
        self._probes: dict[str, HealthProbe] = {}

    def register(self, name: str, probe: HealthProbe) -> None:
        if name in self._probes:
            raise ValueError(f"health probe {name!r} is already registered")
        self._probes[name] = probe
        logger.debug("health_probe_registered", probe=name)

    def unregister(self, name: str) -> None:
        self._probes.pop(name, None)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._probes)

    async def _run_one(self, name: str, probe: HealthProbe) -> ComponentHealth:
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                result = await probe()
        except TimeoutError:
            elapsed = (time.perf_counter() - started) * 1000
            logger.warning("health_probe_timeout", probe=name, timeout=self._timeout_seconds)
            return ComponentHealth(
                name=name,
                status=HealthStatus.UNHEALTHY,
                detail=f"probe timed out after {self._timeout_seconds}s",
                latency_ms=round(elapsed, 2),
            )
        except Exception as exc:  # noqa: BLE001 — a broken probe must not break /health
            elapsed = (time.perf_counter() - started) * 1000
            logger.warning("health_probe_failed", probe=name, exc_info=exc)
            return ComponentHealth(
                name=name,
                status=HealthStatus.UNHEALTHY,
                detail=f"{type(exc).__name__}: {exc}",
                latency_ms=round(elapsed, 2),
            )

        if result.latency_ms is None:
            elapsed = (time.perf_counter() - started) * 1000
            return ComponentHealth(
                name=result.name,
                status=result.status,
                detail=result.detail,
                latency_ms=round(elapsed, 2),
            )
        return result

    async def run_all(self) -> list[ComponentHealth]:
        """Run every probe concurrently. Order of the result matches registration order."""
        if not self._probes:
            return []
        results = await asyncio.gather(
            *(self._run_one(name, probe) for name, probe in self._probes.items())
        )
        return list(results)


def aggregate_status(components: list[ComponentHealth]) -> HealthStatus:
    """Roll component statuses into one. Worst status wins."""
    if any(component.status is HealthStatus.UNHEALTHY for component in components):
        return HealthStatus.UNHEALTHY
    if any(component.status is HealthStatus.DEGRADED for component in components):
        return HealthStatus.DEGRADED
    return HealthStatus.OK
