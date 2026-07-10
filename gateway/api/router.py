"""Top-level router. Later phases mount their routers here and nowhere else."""

from __future__ import annotations

from fastapi import APIRouter

from gateway.api.routes import health, plugins

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(plugins.router)

# Provider proxy routers are mounted per configured provider in `create_app`, since
# which providers exist and where they mount is decided by configuration.

__all__ = ["api_router"]
