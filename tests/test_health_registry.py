"""Unit tests for the health-check registry.

The contract that matters: a misbehaving probe degrades its own component and
nothing else. It cannot hang, crash, or slow down the readiness endpoint.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway.health import (
    ComponentHealth,
    HealthRegistry,
    HealthStatus,
    aggregate_status,
)


def _ok(name: str) -> ComponentHealth:
    return ComponentHealth(name=name, status=HealthStatus.OK)


async def test_empty_registry_reports_ok() -> None:
    registry = HealthRegistry()
    components = await registry.run_all()
    assert components == []
    assert aggregate_status(components) is HealthStatus.OK


async def test_healthy_probe_is_timed() -> None:
    registry = HealthRegistry()

    async def probe() -> ComponentHealth:
        return _ok("postgres")

    registry.register("postgres", probe)
    (component,) = await registry.run_all()

    assert component.status is HealthStatus.OK
    assert component.latency_ms is not None
    assert component.latency_ms >= 0


async def test_probe_may_report_its_own_latency() -> None:
    registry = HealthRegistry()

    async def probe() -> ComponentHealth:
        return ComponentHealth(name="redis", status=HealthStatus.OK, latency_ms=42.0)

    registry.register("redis", probe)
    (component,) = await registry.run_all()
    assert component.latency_ms == 42.0


async def test_raising_probe_becomes_unhealthy_and_does_not_propagate() -> None:
    registry = HealthRegistry()

    async def probe() -> ComponentHealth:
        raise RuntimeError("connection refused")

    registry.register("postgres", probe)
    (component,) = await registry.run_all()

    assert component.status is HealthStatus.UNHEALTHY
    assert component.detail is not None
    assert "connection refused" in component.detail
    assert "RuntimeError" in component.detail


async def test_hanging_probe_is_bounded_by_the_timeout() -> None:
    registry = HealthRegistry(timeout_seconds=0.02)

    async def probe() -> ComponentHealth:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    registry.register("slow", probe)
    (component,) = await registry.run_all()

    assert component.status is HealthStatus.UNHEALTHY
    assert component.detail is not None
    assert "timed out" in component.detail


async def test_one_bad_probe_does_not_hide_the_healthy_ones() -> None:
    registry = HealthRegistry(timeout_seconds=0.05)

    async def good() -> ComponentHealth:
        return _ok("good")

    async def bad() -> ComponentHealth:
        raise ValueError("boom")

    registry.register("good", good)
    registry.register("bad", bad)
    by_name = {component.name: component for component in await registry.run_all()}

    assert by_name["good"].status is HealthStatus.OK
    assert by_name["bad"].status is HealthStatus.UNHEALTHY


async def test_probes_run_concurrently_not_serially() -> None:
    registry = HealthRegistry(timeout_seconds=1.0)

    async def slow(name: str) -> ComponentHealth:
        await asyncio.sleep(0.05)
        return _ok(name)

    for name in ("a", "b", "c", "d"):
        registry.register(name, lambda n=name: slow(n))  # type: ignore[misc]

    loop = asyncio.get_running_loop()
    started = loop.time()
    components = await registry.run_all()
    elapsed = loop.time() - started

    assert len(components) == 4
    # Serial execution would take ~0.20s; concurrent takes ~0.05s.
    assert elapsed < 0.15


def test_duplicate_registration_is_a_programming_error() -> None:
    registry = HealthRegistry()

    async def probe() -> ComponentHealth:
        return _ok("x")

    registry.register("x", probe)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("x", probe)


def test_unregister_is_idempotent() -> None:
    registry = HealthRegistry()
    registry.unregister("never-registered")
    assert registry.names == ()


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], HealthStatus.OK),
        ([HealthStatus.OK, HealthStatus.OK], HealthStatus.OK),
        ([HealthStatus.OK, HealthStatus.DEGRADED], HealthStatus.DEGRADED),
        ([HealthStatus.DEGRADED, HealthStatus.UNHEALTHY], HealthStatus.UNHEALTHY),
        ([HealthStatus.UNHEALTHY, HealthStatus.OK], HealthStatus.UNHEALTHY),
    ],
)
def test_worst_status_wins(statuses: list[HealthStatus], expected: HealthStatus) -> None:
    components = [
        ComponentHealth(name=f"c{index}", status=status) for index, status in enumerate(statuses)
    ]
    assert aggregate_status(components) is expected


def test_only_unhealthy_blocks_readiness() -> None:
    assert ComponentHealth("a", HealthStatus.UNHEALTHY).blocks_readiness is True
    assert ComponentHealth("a", HealthStatus.DEGRADED).blocks_readiness is False
    assert ComponentHealth("a", HealthStatus.OK).blocks_readiness is False
