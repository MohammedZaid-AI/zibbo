"""The proxy route.

One catch-all handler per provider, not one handler per endpoint. The gateway
does not model ``chat/completions`` or ``embeddings`` because it does not modify
them — it only needs to know where they live. Anything OpenAI adds tomorrow is
proxied today.

Optimization enters here as a single call. The route knows a pipeline exists; it
does not know HTML exists, nor that the pipeline chose an HTML transformer.

Excluded from the OpenAPI schema on purpose: a ``{path:path}`` wildcard documents
nothing useful, and publishing it would imply the gateway validates payloads it
deliberately passes through untouched.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request, Response

from gateway.api.deps import PipelineDep, ProviderRegistryDep, ProxyServiceDep
from gateway.middleware.request_context import (
    OPTIMIZATION_HEADER,
    TOKENS_SAVED_HEADER,
)
from gateway.optimizers import TransformationRequest

PROXIED_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]


def create_proxy_router(*, provider_name: str, prefix: str) -> APIRouter:
    """Mount ``provider_name`` at ``prefix``.

    Phase 6 calls this again for Anthropic. The only per-provider inputs are a
    name and a path prefix, which is the whole point of the Provider abstraction.
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
            )
        )

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

        return response

    return router
