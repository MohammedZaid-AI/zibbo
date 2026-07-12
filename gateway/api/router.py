"""Top-level router. Later phases mount their routers here and nowhere else."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from gateway.api.deps import require_local
from gateway.api.routes import cache, health, internal, plugins

api_router = APIRouter()
api_router.include_router(health.router)

# The /internal/* group is the plugin's control surface. One shared loopback guard
# covers every route in it — status, stats, cache, plugins, enable/disable, doctor — so
# no individual internal route can forget to gate itself.
internal_router = APIRouter(dependencies=[Depends(require_local)])
internal_router.include_router(internal.router)
internal_router.include_router(cache.router)
internal_router.include_router(plugins.router)
api_router.include_router(internal_router)

# Provider proxy routers are mounted per configured provider in `create_app`, since
# which providers exist and where they mount is decided by configuration.

__all__ = ["api_router"]
