"""Transport: send an :class:`UpstreamRequest`, relay the response.

Provider-agnostic by construction. Every provider gets streaming, header
preservation, and error mapping for free.

Two response paths, chosen by whether the caller asked for a stream:

* **Buffered.** Read the whole body, relay it. The bytes are never re-serialized,
  so what the provider sent is exactly what the caller receives — key order,
  whitespace and all.
* **Streaming.** Relay chunks as they arrive, closing the upstream response when
  the caller disconnects. Nothing is buffered, so time-to-first-token is the
  provider's, plus a proxy hop.

An upstream **HTTP error is not an exception here.** OpenAI's 400 body is already
the envelope its SDK expects, so it is relayed verbatim, status and all. Only
failures that produce no HTTP response — connect refused, timeout — become
gateway errors, and those are shapes the provider itself can never return.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import httpx
from starlette.responses import Response, StreamingResponse

from gateway.errors import UpstreamError, UpstreamTimeoutError
from gateway.logging import get_logger
from gateway.providers.headers import filter_response_headers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from structlog.stdlib import BoundLogger

    from gateway.providers.base import Provider, UpstreamRequest

logger = get_logger(__name__)

# Tells nginx and friends not to buffer an SSE body, which would defeat streaming.
_ACCEL_BUFFERING_HEADER = "x-accel-buffering"


class ProxyService:
    """Relays requests to a provider and responses back to the caller."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def forward(
        self,
        *,
        provider: Provider,
        method: str,
        path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> Response:
        upstream = provider.build_request(
            method=method, path=path, query=query, headers=headers, content=body
        )
        log = logger.bind(
            provider=provider.name,
            upstream_path=f"/{path.lstrip('/')}",
            stream=upstream.stream,
            model=upstream.model,
        )

        if upstream.stream:
            return await self._forward_streaming(provider, upstream, log)
        return await self._forward_buffered(provider, upstream, log)

    # -- Response paths ----------------------------------------------------

    async def _forward_buffered(
        self, provider: Provider, upstream: UpstreamRequest, log: BoundLogger
    ) -> Response:
        started = time.perf_counter()
        try:
            response = await self._client.request(
                upstream.method,
                upstream.url,
                headers=upstream.headers,
                # A bare `b""` would make httpx send `content-length: 0` on a GET.
                content=upstream.content or None,
            )
        except httpx.TimeoutException as exc:
            raise _timeout_error(provider, exc) from exc
        except httpx.TransportError as exc:
            raise _transport_error(provider, exc) from exc

        log.info(
            "upstream_completed",
            upstream_status=response.status_code,
            upstream_duration_ms=_elapsed_ms(started),
            response_bytes=len(response.content),
        )
        return _relay(response.status_code, response.headers, content=response.content)

    async def _forward_streaming(
        self, provider: Provider, upstream: UpstreamRequest, log: BoundLogger
    ) -> Response:
        started = time.perf_counter()
        request = self._client.build_request(
            upstream.method,
            upstream.url,
            headers=upstream.headers,
            content=upstream.content or None,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise _timeout_error(provider, exc) from exc
        except httpx.TransportError as exc:
            raise _transport_error(provider, exc) from exc

        # A stream that fails before it starts is a plain JSON error, not an SSE
        # frame. The SDK is still waiting on `Content-Type: application/json`, so
        # buffer the error body and relay it as an ordinary response.
        if response.status_code >= httpx.codes.BAD_REQUEST:
            body = await response.aread()
            await response.aclose()
            log.info(
                "upstream_stream_rejected",
                upstream_status=response.status_code,
                upstream_duration_ms=_elapsed_ms(started),
            )
            return _relay(response.status_code, response.headers, content=body)

        log.info(
            "upstream_stream_opened",
            upstream_status=response.status_code,
            time_to_headers_ms=_elapsed_ms(started),
        )
        return _relay(
            response.status_code,
            response.headers,
            iterator=self._drain(response, log, started),
        )

    @staticmethod
    async def _drain(
        response: httpx.Response, log: BoundLogger, started: float
    ) -> AsyncIterator[bytes]:
        """Yield upstream chunks, always releasing the connection.

        The ``finally`` matters more than the loop. If the caller disconnects
        mid-stream, Starlette cancels this generator; without the close, the
        upstream connection leaks out of the pool and the pool eventually starves.
        """
        forwarded = 0
        try:
            async for chunk in response.aiter_bytes():
                forwarded += len(chunk)
                yield chunk
        finally:
            await response.aclose()
            log.info(
                "upstream_stream_closed",
                response_bytes=forwarded,
                upstream_duration_ms=_elapsed_ms(started),
            )


# -- Helpers ---------------------------------------------------------------


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)


def _relay(
    status_code: int,
    upstream_headers: httpx.Headers,
    *,
    content: bytes | None = None,
    iterator: AsyncIterator[bytes] | None = None,
) -> Response:
    """Build the caller-facing response, carrying upstream's headers across."""
    response: Response
    if iterator is not None:
        response = StreamingResponse(iterator, status_code=status_code)
    else:
        response = Response(content=content, status_code=status_code)

    # `append`, not assignment: repeated headers such as set-cookie must survive.
    # Starlette has already computed content-length, which is why it is stripped.
    for key, value in filter_response_headers(upstream_headers):
        response.headers.append(key, value)

    if iterator is not None:
        response.headers[_ACCEL_BUFFERING_HEADER] = "no"
    return response


def _timeout_error(provider: Provider, exc: httpx.TimeoutException) -> UpstreamTimeoutError:
    return UpstreamTimeoutError(
        f"The upstream provider {provider.name!r} did not respond in time.",
        context={"provider": provider.name, "cause": type(exc).__name__},
    )


def _transport_error(provider: Provider, exc: httpx.TransportError) -> UpstreamError:
    return UpstreamError(
        f"Could not reach the upstream provider {provider.name!r}.",
        context={"provider": provider.name, "cause": type(exc).__name__},
    )
