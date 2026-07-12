"""The proxy route.

One catch-all handler per provider, not one handler per endpoint. The gateway does
not model ``chat/completions`` or ``messages`` because it does not modify them — it
only needs to know where they live. Anything a provider adds tomorrow is proxied
today.

The handler holds **no provider logic**. It resolves its provider by name, reads that
provider's optimization policy and adapters, and hands both to the shared pipeline.
Everything provider-specific — auth, endpoints, schema, error shape — lives on the
provider object.

Excluded from the OpenAPI schema on purpose: a ``{path:path}`` wildcard documents
nothing useful, and publishing it would imply the gateway validates payloads it
deliberately passes through untouched.
"""

from __future__ import annotations

import httpx
import structlog
from fastapi import APIRouter, Request, Response

from gateway.analytics import event_from_report
from gateway.api.deps import PipelineDep, ProviderRegistryDep, ProxyServiceDep
from gateway.logging import get_logger
from gateway.middleware.request_context import (
    CACHE_HEADER,
    OPTIMIZATION_HEADER,
    TOKENS_SAVED_HEADER,
)
from gateway.optimizers import TransformationReport, TransformationRequest

logger = get_logger(__name__)

PROXIED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def create_proxy_router(*, provider_name: str, prefix: str) -> APIRouter:
    """Mount ``provider_name`` at ``prefix``.

    Called once per configured provider. The only per-provider inputs are a name and
    a path prefix, which is the whole point of the Provider abstraction.
    """
    router = APIRouter(prefix=prefix, tags=[provider_name])

    @router.api_route(
        "/{upstream_path:path}",
        methods=PROXIED_METHODS,
        include_in_schema=False,
        response_model=None,
    )
    async def proxy(
        upstream_path: str,
        request: Request,
        registry: ProviderRegistryDep,
        proxy_service: ProxyServiceDep,
        pipeline: PipelineDep,
    ) -> Response:
        provider = registry.get(provider_name)
        policy = request.app.state.provider_policies[provider_name]

        # Bound here rather than passed down: every log line emitted while handling
        # this request — the access log included — picks these up from contextvars.
        structlog.contextvars.bind_contextvars(
            provider=provider.name,
            endpoint=f"/{upstream_path.lstrip('/')}",
        )

        # Fully buffered. Streaming *request* bodies would let a huge file upload
        # pass through without touching memory, but the pipeline must see the whole
        # body to optimize it. See docs/COMPATIBILITY.md.
        body = await request.body()

        report = await pipeline.transform(
            TransformationRequest(
                method=request.method,
                path=upstream_path,
                content_type=request.headers.get("content-type", ""),
                body=body,
            ),
            policy=policy,
            adapters=provider.adapters,
        )
        _bind_optimization_context(report)
        _record_analytics(request, report, provider=provider.name, endpoint=upstream_path)

        # `.raw` rather than the mapping view: a client may send a header twice,
        # and collapsing the pair would change the request the provider sees.
        response = await proxy_service.forward(
            provider=provider,
            method=request.method,
            path=upstream_path,
            query=request.url.query,
            headers=httpx.Headers(request.headers.raw),
            body=report.body,
        )

        # Cheap enough to always report; the dashboard reads these in Phase 5.
        if report.applied:
            response.headers[OPTIMIZATION_HEADER] = "applied"
            response.headers[TOKENS_SAVED_HEADER] = str(report.tokens_saved)
        elif report.skip_reason is not None:
            response.headers[OPTIMIZATION_HEADER] = f"skipped:{report.skip_reason.value}"

        # Whether the work behind this request was reused from the cache: `hit` (all
        # segments), `miss` (none), or `partial` (a mix). Absent when nothing was
        # eligible to cache.
        if report.cache_status is not None:
            response.headers[CACHE_HEADER] = report.cache_status

        return response

    return router


def _record_analytics(
    request: Request, report: TransformationReport, *, provider: str, endpoint: str
) -> None:
    """Fold this request's outcome into the analytics engine. Never breaks the request.

    Metadata only — the event carries counts and names, never body content. Analytics
    is an observation of the request, so its failure must not fail the request."""
    engine = getattr(request.app.state, "analytics", None)
    if engine is None:
        return
    try:
        engine.record(event_from_report(report, provider=provider, endpoint=endpoint))
    except Exception:  # noqa: BLE001 — analytics is best-effort, never load-bearing
        logger.warning("analytics_record_failed")


def _bind_optimization_context(report: TransformationReport) -> None:
    """Metadata only. Never a byte of what the user sent."""
    structlog.contextvars.bind_contextvars(
        optimization_applied=report.applied,
        tokens_before=report.original_token_count,
        tokens_after=report.transformed_token_count,
        tokens_saved=report.tokens_saved,
        bytes_before=report.original_size_bytes,
        bytes_after=report.transformed_size_bytes,
        transformers=report.transformers_used,
        skip_reason=report.skip_reason.value if report.skip_reason else None,
    )
