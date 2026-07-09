"""Wire schemas for the health endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from gateway.config import Environment
from gateway.health import ComponentHealth, HealthStatus


class LivenessResponse(BaseModel):
    """Answer to 'is the process running?' — never touches a dependency."""

    model_config = ConfigDict(frozen=True)

    status: str = Field(default="alive", examples=["alive"])


class ComponentHealthResponse(BaseModel):
    """Per-dependency probe result."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: HealthStatus
    detail: str | None = None
    latency_ms: float | None = None

    @classmethod
    def from_domain(cls, component: ComponentHealth) -> ComponentHealthResponse:
        return cls(
            name=component.name,
            status=component.status,
            detail=component.detail,
            latency_ms=component.latency_ms,
        )


class ReadinessResponse(BaseModel):
    """Answer to 'can this instance serve traffic?' — probes every dependency."""

    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    components: list[ComponentHealthResponse] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Human-facing service summary."""

    model_config = ConfigDict(frozen=True)

    status: HealthStatus
    service: str
    version: str
    environment: Environment
    uptime_seconds: float
