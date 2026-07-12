"""The /internal/* control and introspection API for the Zibbo plugin.

Read endpoints (status, stats, version, logs) describe the deployment; write endpoints
(enable, disable, benchmark, doctor) act on it. All of it is metadata and control — no
endpoint here reads or returns request content.

Access is gated to loopback by ``require_local`` (applied to the whole /internal group in
``api.router``); this module assumes that guard has already run.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request

from gateway.api.deps import (
    AnalyticsDep,
    CacheDep,
    PipelineDep,
    RuntimeControlDep,
    SettingsDep,
)
from gateway.api.schemas.internal import (
    INTERNAL_API_VERSION,
    BenchmarkRequest,
    BenchmarkResponse,
    DoctorCheck,
    DoctorResponse,
    LogEvent,
    LogsResponse,
    ProviderInfo,
    StatsResponse,
    StatusResponse,
    ToggleResponse,
    TopTransformer,
    VersionResponse,
    WindowStatsModel,
)

if TYPE_CHECKING:
    from gateway.analytics.models import WindowStats

router = APIRouter(prefix="/internal", tags=["internal"])


# -- Read --------------------------------------------------------------------


@router.get("/status", response_model=StatusResponse, summary="Gateway status")
async def status(
    request: Request,
    settings: SettingsDep,
    cache: CacheDep,
    control: RuntimeControlDep,
) -> StatusResponse:
    state = request.app.state
    providers = [
        ProviderInfo(name=entry.provider.name, prefix=entry.prefix)
        for entry in state.mounted_providers
    ]
    transformers = list(getattr(state.transformer_registry, "names", ()))
    return StatusResponse(
        name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
        uptime_seconds=round(time.monotonic() - state.started_at, 3),
        optimization_enabled=control.optimization_enabled,
        pipeline_active=control.optimization_enabled,
        cache_enabled=cache.enabled,
        cache_backend=cache.backend_name,
        documents_enabled=state.documents.enabled,
        transformers=transformers,
        providers=providers,
    )


@router.get("/stats", response_model=StatsResponse, summary="Optimization statistics")
async def stats(analytics: AnalyticsDep, settings: SettingsDep) -> StatsResponse:
    rate = settings.analytics_cost_per_million_tokens
    return StatsResponse(
        date=analytics.today_date,
        today=_window_model(analytics.today(), rate),
        all_time=_window_model(analytics.all_time(), rate),
    )


@router.get("/version", response_model=VersionResponse, summary="Version")
async def version(settings: SettingsDep) -> VersionResponse:
    return VersionResponse(
        gateway_version=settings.app_version,
        internal_api_version=INTERNAL_API_VERSION,
        app_name=settings.app_name,
    )


@router.get("/logs", response_model=LogsResponse, summary="Recent optimization activity")
async def logs(analytics: AnalyticsDep, limit: int = 20) -> LogsResponse:
    limit = max(1, min(limit, 100))
    events = [
        LogEvent(
            timestamp=event.timestamp,
            provider=event.provider,
            endpoint=event.endpoint,
            applied=event.applied,
            skip_reason=event.skip_reason,
            content_types=list(event.content_types),
            transformers=list(event.transformers),
            steps=list(event.steps),
            tokens_before=event.tokens_before,
            tokens_after=event.tokens_after,
            tokens_saved=event.tokens_saved,
            cache_status=event.cache_status,
            execution_time_ms=event.execution_time_ms,
        )
        for event in analytics.recent(limit)
    ]
    return LogsResponse(count=len(events), events=events)


# -- Write -------------------------------------------------------------------


@router.post("/enable", response_model=ToggleResponse, summary="Enable transformations")
async def enable(control: RuntimeControlDep) -> ToggleResponse:
    return ToggleResponse(optimization_enabled=control.set_optimization_enabled(True))


@router.post("/disable", response_model=ToggleResponse, summary="Disable transformations")
async def disable(control: RuntimeControlDep) -> ToggleResponse:
    return ToggleResponse(optimization_enabled=control.set_optimization_enabled(False))


@router.post("/benchmark", response_model=BenchmarkResponse, summary="Replay through the pipeline")
async def benchmark(
    body: BenchmarkRequest, pipeline: PipelineDep, analytics: AnalyticsDep
) -> BenchmarkResponse:
    """Show what the pipeline does to some content, without forwarding anything.

    With ``content``: run it through the real transform path (detection, transformers,
    cache) and report. Without it: replay the *last* request's recorded metadata, which
    is all that is kept — request bodies are never stored.
    """
    if body.content is not None:
        result = pipeline.preview(body.content, model=body.model)
        return BenchmarkResponse(
            source="provided",
            content_type=result.detected_content_type.value,
            original_tokens=result.original_token_count,
            optimized_tokens=result.transformed_token_count,
            reduction_pct=result.token_reduction_pct,
            transformers=list(result.transformations_applied),
            cache_used=result.cache_hit,
            processing_time_ms=result.execution_time_ms,
        )

    last = analytics.last_event
    if last is None:
        return BenchmarkResponse(
            source="none",
            content_type=None,
            original_tokens=0,
            optimized_tokens=0,
            reduction_pct=0.0,
            transformers=[],
            cache_used=False,
            processing_time_ms=0.0,
            note="No request has been optimized yet; supply `content` to benchmark a sample.",
        )
    reduction = (
        round(last.tokens_saved / last.tokens_before * 100, 2) if last.tokens_before else 0.0
    )
    return BenchmarkResponse(
        source="last_request",
        content_type=last.content_types[0] if last.content_types else None,
        original_tokens=last.tokens_before,
        optimized_tokens=last.tokens_after,
        reduction_pct=reduction,
        transformers=list(last.transformers),
        cache_used=last.cache_hits > 0,
        processing_time_ms=last.execution_time_ms,
        note="Replayed from the last request's stored metadata; not re-sent upstream.",
    )


@router.post("/doctor", response_model=DoctorResponse, summary="Diagnostics")
async def doctor(
    request: Request,
    settings: SettingsDep,
    cache: CacheDep,
    control: RuntimeControlDep,
) -> DoctorResponse:
    checks: list[DoctorCheck] = []

    checks.append(
        DoctorCheck(name="gateway", status="ok", detail=f"running {settings.app_version}")
    )

    checks.append(
        DoctorCheck(
            name="optimization",
            status="ok" if control.optimization_enabled else "warn",
            detail="enabled" if control.optimization_enabled else "disabled",
            fix=None if control.optimization_enabled else "POST /internal/enable to turn it on",
        )
    )

    if not cache.enabled:
        checks.append(
            DoctorCheck(
                name="cache",
                status="warn",
                detail="disabled",
                fix="set ZIBBO_CACHE_ENABLED=true",
            )
        )
    elif cache.probe():
        checks.append(
            DoctorCheck(name="cache", status="ok", detail=f"{cache.backend_name} reachable")
        )
    else:
        checks.append(
            DoctorCheck(
                name="cache",
                status="fail",
                detail=f"{cache.backend_name} unreachable",
                fix="check ZIBBO_REDIS_URL and that Redis is running",
            )
        )

    providers = [entry.provider.name for entry in request.app.state.mounted_providers]
    checks.append(
        DoctorCheck(
            name="providers",
            status="ok" if providers else "warn",
            detail=", ".join(providers) if providers else "none configured",
            fix=None if providers else "enable a provider in configuration",
        )
    )

    healthy = all(check.status != "fail" for check in checks)
    return DoctorResponse(healthy=healthy, checks=checks)


# -- Helpers -----------------------------------------------------------------


def _window_model(stats: WindowStats, cost_per_million: float) -> WindowStatsModel:
    top = stats.top_transformer
    estimated_cost = (
        round(stats.tokens_saved / 1_000_000 * cost_per_million, 4) if cost_per_million else None
    )
    return WindowStatsModel(
        requests=stats.requests,
        optimized=stats.optimized,
        skipped=stats.skipped,
        tokens_before=stats.tokens_before,
        tokens_after=stats.tokens_after,
        tokens_saved=stats.tokens_saved,
        token_reduction_pct=stats.token_reduction_pct,
        bytes_saved=stats.bytes_saved,
        cache_hit_rate=stats.cache_hit_rate,
        transformations=stats.transformations,
        avg_latency_ms=stats.avg_latency_ms,
        top_transformer=(
            TopTransformer(name=top.name, count=top.count, tokens_saved=top.tokens_saved)
            if top is not None
            else None
        ),
        estimated_cost_usd=estimated_cost,
    )
