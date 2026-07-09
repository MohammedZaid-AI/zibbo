"""Top-level router. Later phases mount their routers here and nowhere else."""

from __future__ import annotations

from fastapi import APIRouter

from gateway.api.routes import health

api_router = APIRouter()
api_router.include_router(health.router)

# Phase 2 mounts the OpenAI-compatible router at /v1.
# Phase 6 mounts the Anthropic-compatible router at /anthropic/v1.
# Phase 4 mounts the analytics router at /internal/analytics.

__all__ = ["api_router"]
