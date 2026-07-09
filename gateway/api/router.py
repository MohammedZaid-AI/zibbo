"""Top-level router. Later phases mount their routers here and nowhere else."""

from __future__ import annotations

from fastapi import APIRouter

from gateway.api.routes import health
from gateway.api.routes.proxy import create_proxy_router
from gateway.providers import OpenAIProvider

api_router = APIRouter()
api_router.include_router(health.router)

# Callers reach OpenAI by pointing `base_url` at `<gateway>/v1`.
api_router.include_router(create_proxy_router(provider_name=OpenAIProvider.name, prefix="/v1"))

# Phase 6 mounts the Anthropic-compatible router at /anthropic/v1.
# Phase 4 mounts the analytics router at /internal/analytics.

__all__ = ["api_router"]
