"""Integration tests for the health endpoints."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from gateway.config import Settings
from gateway.health import ComponentHealth, HealthStatus

pytestmark = pytest.mark.integration


async def test_liveness_never_touches_dependencies(app: FastAPI, client: AsyncClient) -> None:
    async def hanging_probe() -> ComponentHealth:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    app.state.health.register("postgres", hanging_probe)

    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}


async def test_health_summary_reports_service_metadata(
    client: AsyncClient, settings: Settings
) -> None:
    response = await client.get("/health")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["service"] == settings.app_name
    assert body["version"] == settings.app_version
    assert body["environment"] == "test"
    assert body["uptime_seconds"] >= 0


async def test_readiness_is_ok_with_no_registered_probes(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "components": []}


async def test_readiness_reports_each_component(app: FastAPI, client: AsyncClient) -> None:
    async def postgres() -> ComponentHealth:
        return ComponentHealth(name="postgres", status=HealthStatus.OK)

    async def redis() -> ComponentHealth:
        return ComponentHealth(name="redis", status=HealthStatus.OK, latency_ms=1.5)

    app.state.health.register("postgres", postgres)
    app.state.health.register("redis", redis)

    response = await client.get("/health/ready")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert {component["name"] for component in body["components"]} == {"postgres", "redis"}
    assert all(component["latency_ms"] is not None for component in body["components"])


async def test_readiness_returns_503_when_a_dependency_is_unhealthy(
    app: FastAPI, client: AsyncClient
) -> None:
    async def postgres() -> ComponentHealth:
        raise ConnectionRefusedError("could not connect to server")

    app.state.health.register("postgres", postgres)

    response = await client.get("/health/ready")
    body = response.json()

    assert response.status_code == 503
    assert body["status"] == "unhealthy"
    assert body["components"][0]["name"] == "postgres"
    assert "could not connect" in body["components"][0]["detail"]


async def test_degraded_dependency_still_serves_traffic(app: FastAPI, client: AsyncClient) -> None:
    """A degraded cache means slower responses, not a dead instance."""

    async def redis() -> ComponentHealth:
        return ComponentHealth(name="redis", status=HealthStatus.DEGRADED, detail="high latency")

    app.state.health.register("redis", redis)

    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
