"""Plugin introspection.

Read-only, and worth having: when a plugin does not fire, the first question is
always "was it even loaded?". This answers it, including for plugins that failed,
with the stage that failed and the exception that caused it.

Mounted under ``/internal`` because it describes the deployment, not the API.
"""

from __future__ import annotations

from fastapi import APIRouter

from gateway.api.deps import PluginManagerDep
from gateway.api.schemas.plugins import PluginListResponse, PluginSummary
from gateway.plugins import PLUGIN_API_VERSION

router = APIRouter(prefix="/internal", tags=["plugins"])


@router.get("/plugins", response_model=PluginListResponse, summary="Loaded plugins")
async def list_plugins(plugins: PluginManagerDep) -> PluginListResponse:
    report = plugins.report()
    return PluginListResponse(
        api_version=PLUGIN_API_VERSION,
        total=len(report.records),
        enabled=list(report.enabled),
        disabled=list(report.disabled),
        failed=[record.name for record in report.failed],
        plugins=[PluginSummary.from_record(record) for record in report.records],
    )
