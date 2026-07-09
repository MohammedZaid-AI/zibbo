"""Liveness, readiness, and service-summary endpoints.

The split matters for orchestrators. Kubernetes restarts a pod that fails
*liveness* and merely removes it from the load balancer when it fails
*readiness*. A gateway whose Postgres is briefly unreachable should stop taking
traffic, not be killed — so liveness must never touch a dependency.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Response, status

from gateway.api.deps import HealthRegistryDep, SettingsDep, StartTimeDep
from gateway.api.schemas.health import (
    ComponentHealthResponse,
    HealthResponse,
    LivenessResponse,
    ReadinessResponse,
)
from gateway.health import HealthStatus, aggregate_status

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service summary",
)
async def health(settings: SettingsDep, started_at: StartTimeDep) -> HealthResponse:
    return HealthResponse(
        status=HealthStatus.OK,
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        uptime_seconds=round(time.monotonic() - started_at, 3),
    )


@router.get(
    "/health/live",
    response_model=LivenessResponse,
    summary="Liveness probe",
)
async def liveness() -> LivenessResponse:
    return LivenessResponse()


@router.get(
    "/health/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "description": "At least one dependency is unhealthy.",
            "model": ReadinessResponse,
        }
    },
)
async def readiness(registry: HealthRegistryDep, response: Response) -> ReadinessResponse:
    components = await registry.run_all()
    overall = aggregate_status(components)

    if overall is HealthStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status=overall,
        components=[ComponentHealthResponse.from_domain(component) for component in components],
    )
