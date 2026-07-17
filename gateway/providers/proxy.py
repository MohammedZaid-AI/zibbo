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

An upstream **HTTP error is not an exception here.** The provider's own 400 body is
already the envelope its SDK expects, so it is relayed verbatim, status and all. Only
failures that produce no HTTP response — connect refused, timeout — become gateway
errors, and those are rendered in *this provider's* envelope so the caller's SDK can
still parse them.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from gateway.errors import ErrorType, UpstreamError, UpstreamTimeoutError
from gateway.logging import get_logger
from gateway.middleware.request_context import get_request_id
from gateway.providers.headers import filter_response_headers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from structlog.stdlib import BoundLogger

    from gateway.providers.base import Provider, UpstreamRequest

logger = get_logger(__name__)

# Tells nginx and friends not to buffer an SSE body, which would defeat streaming.
_ACCEL_BUFFERING_HEADER = "x-accel-buffering"

# Connection-level failures where the request provably never reached the provider:
# the pool handed back a keepalive connection the server had already closed (its FIN
# was in flight when the pool last checked the socket), or the connect itself failed.
# Anthropic sits behind Cloudflare, which drops idle connections aggressively, so
# under Claude Code's bursty traffic a fraction of sends land on a dead socket and
# httpx raises *before a single request byte is written*. Retrying is safe precisely
# because nothing was sent — "server disconnected without sending a response" means
# the request was never processed. This is the difference between an intermittent 502
# and a clean 200. TimeoutException is deliberately excluded: a slow server may still
# be processing the request, so a retry there could double-submit.
_RETRIABLE_CONNECT_ERRORS = (httpx.ConnectError, httpx.RemoteProtocolError)


@dataclass(frozen=True, slots=True)
class ProxyMetricsSnapshot:
    """A point-in-time read of the transport counters. Counts only, never content."""

    successful_retries: int
    failed_retries: int
    transport_failures: int


class ProxyMetrics:
    """Process-lifetime transport counters, separate from optimization analytics.

    A retry or a transport failure is a property of the *connection*, not of any
    request's optimization outcome, so it lives here rather than in the analytics
    engine. Thread-safe: incremented from the request path (sometimes a worker
    thread), read from ``/internal/stats`` on the event loop.
    """

    __slots__ = ("_lock", "_successful_retries", "_failed_retries", "_transport_failures")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._successful_retries = 0
        self._failed_retries = 0
        self._transport_failures = 0

    def record_retry_success(self) -> None:
        """A stale connection was recovered: a retry send opened the response."""
        with self._lock:
            self._successful_retries += 1

    def record_retry_exhausted(self) -> None:
        """Every allowed retry was spent and the connection still would not open."""
        with self._lock:
            self._failed_retries += 1

    def record_transport_failure(self) -> None:
        """A transport error was surfaced to the caller (timeout, or a spent retry)."""
        with self._lock:
            self._transport_failures += 1

    def snapshot(self) -> ProxyMetricsSnapshot:
        with self._lock:
            return ProxyMetricsSnapshot(
                successful_retries=self._successful_retries,
                failed_retries=self._failed_retries,
                transport_failures=self._transport_failures,
            )


class ProxyService:
    """Relays requests to a provider and responses back to the caller."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        reconnect_attempts: int = 1,
        metrics: ProxyMetrics | None = None,
    ) -> None:
        self._client = client
        # Extra attempts after the first, only for provably-unsent connection drops.
        self._reconnect_attempts = max(0, reconnect_attempts)
        self._metrics = metrics or ProxyMetrics()

    @property
    def metrics(self) -> ProxyMetrics:
        return self._metrics

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

    async def _open_upstream(
        self, provider: Provider, upstream: UpstreamRequest, log: BoundLogger
    ) -> httpx.Response:
        """Establish the upstream response, retrying a dropped connection.

        The response is returned with its body **unread**. Every failure raised here
        is therefore pre-response — no chunk has been consumed — which is what makes
        the retry safe: a request that never got a byte back was never processed, so
        re-sending it cannot double-submit. Both the buffered and streaming paths go
        through this one boundary so the safety argument holds for both.
        """
        attempts = self._reconnect_attempts + 1
        retried = False
        for attempt in range(1, attempts + 1):
            # Rebuilt each attempt: a request whose content stream was consumed by a
            # failed send cannot be replayed as-is.
            request = self._client.build_request(
                upstream.method,
                upstream.url,
                headers=upstream.headers,
                # A bare `b""` would make httpx send `content-length: 0` on a GET.
                content=upstream.content or None,
            )
            attempt_started = time.perf_counter()
            try:
                response = await self._client.send(request, stream=True)
            except _RETRIABLE_CONNECT_ERRORS as exc:
                if attempt >= attempts:
                    self._metrics.record_retry_exhausted()
                    self._metrics.record_transport_failure()
                    log.warning(
                        "upstream_connection_exhausted",
                        attempts=attempt,
                        reason=type(exc).__name__,
                        request_id=get_request_id(),
                        elapsed_ms=_elapsed_ms(attempt_started),
                    )
                    raise _transport_error(provider, exc) from exc
                retried = True
                # No secret is logged: reason is the exception class name, request_id is
                # a generated UUID. Never a header value, never a byte of the body.
                log.warning(
                    "upstream_connection_retry",
                    attempt=attempt,
                    reason=type(exc).__name__,
                    request_id=get_request_id(),
                    elapsed_ms=_elapsed_ms(attempt_started),
                )
                continue
            except httpx.TimeoutException as exc:
                self._metrics.record_transport_failure()
                raise _timeout_error(provider, exc) from exc
            except httpx.TransportError as exc:
                self._metrics.record_transport_failure()
                raise _transport_error(provider, exc) from exc

            if retried:
                self._metrics.record_retry_success()
                log.info(
                    "upstream_connection_recovered",
                    attempt=attempt,
                    retry_success=True,
                    request_id=get_request_id(),
                    elapsed_ms=_elapsed_ms(attempt_started),
                )
            return response
        raise AssertionError("unreachable: loop returns or raises on the final attempt")

    async def _forward_buffered(
        self, provider: Provider, upstream: UpstreamRequest, log: BoundLogger
    ) -> Response:
        started = time.perf_counter()
        response = await self._open_upstream(provider, upstream, log)
        try:
            body = await response.aread()
        except httpx.TimeoutException as exc:
            raise _timeout_error(provider, exc) from exc
        except httpx.TransportError as exc:
            raise _transport_error(provider, exc) from exc
        finally:
            await response.aclose()

        log.info(
            "upstream_completed",
            upstream_status=response.status_code,
            upstream_duration_ms=_elapsed_ms(started),
            response_bytes=len(body),
        )
        return _relay(response.status_code, response.headers, content=body)

    async def _forward_streaming(
        self, provider: Provider, upstream: UpstreamRequest, log: BoundLogger
    ) -> Response:
        started = time.perf_counter()
        response = await self._open_upstream(provider, upstream, log)

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
        relay = _StreamRelay(response, log, started, provider)
        return _relay(
            response.status_code,
            response.headers,
            iterator=relay,
            # Belt and braces: Starlette runs this once the response is finished,
            # however it finished. `aclose` is idempotent.
            background=BackgroundTask(relay.aclose),
        )


class _StreamRelay:
    """Relays upstream chunks to the caller and owns the upstream connection.

    An explicit iterator rather than an async generator, and the distinction is the
    whole point. A generator's ``finally`` only runs if the generator was *started*;
    calling ``aclose()`` on one that never reached its first ``yield`` is a silent
    no-op. A caller that disconnects between the response headers and the first
    chunk would therefore leak an upstream connection — slowly, invisibly, until the
    pool starved. Here ``aclose`` closes the response whether iteration began or not.

    It also handles the upstream breaking mid-stream. Headers are already sent, so no
    HTTP error can be returned, and stopping silently is the worst outcome: the
    caller's SDK sees a clean end-of-stream and hands back a truncated answer as if
    it were complete. Instead a final error frame is emitted, framed by the provider
    (``provider.stream_error_frame``) so its SDK's stream decoder raises from it
    rather than treating it as silent data loss.
    """

    def __init__(
        self, response: httpx.Response, log: BoundLogger, started: float, provider: Provider
    ) -> None:
        self._response = response
        self._log = log
        self._started = started
        self._provider = provider
        self._chunks: AsyncIterator[bytes] | None = None
        self._forwarded = 0
        self._exhausted = False
        self._closed = False

    def __aiter__(self) -> _StreamRelay:
        return self

    async def __anext__(self) -> bytes:
        if self._exhausted:
            raise StopAsyncIteration
        if self._chunks is None:
            self._chunks = self._response.aiter_bytes()

        try:
            chunk = await anext(self._chunks)
        except StopAsyncIteration:
            self._exhausted = True
            await self.aclose()
            raise
        except httpx.TimeoutException as exc:
            return await self._fail(
                exc, "The upstream provider stopped responding mid-stream.", "upstream_timeout"
            )
        except httpx.TransportError as exc:
            return await self._fail(
                exc, "The upstream provider's response ended unexpectedly.", "upstream_error"
            )
        except BaseException:
            # Cancellation and GeneratorExit land here: the caller hung up, or the
            # server is shutting down. `shield` lets the close finish even though
            # this task is being torn down — otherwise the cancellation would
            # interrupt `aclose` and strand the connection.
            await asyncio.shield(self.aclose())
            raise

        self._forwarded += len(chunk)
        return chunk

    async def _fail(self, exc: Exception, message: str, code: str) -> bytes:
        self._log.warning(
            "upstream_stream_failed",
            cause=type(exc).__name__,
            code=code,
            response_bytes=self._forwarded,
        )
        self._exhausted = True
        await self.aclose()
        payload = self._provider.error_envelope.render(
            message=message,
            error_type=ErrorType.UPSTREAM,
            code=code,
            param=None,
            request_id=get_request_id(),
        )
        return self._provider.stream_error_frame(payload)

    async def aclose(self) -> None:
        """Release the upstream connection. Safe to call any number of times."""
        if self._closed:
            return
        self._closed = True
        await self._response.aclose()
        self._log.info(
            "upstream_stream_closed",
            response_bytes=self._forwarded,
            upstream_duration_ms=_elapsed_ms(self._started),
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
    background: BackgroundTask | None = None,
) -> Response:
    """Build the caller-facing response, carrying upstream's headers across."""
    response: Response
    if iterator is not None:
        response = StreamingResponse(iterator, status_code=status_code, background=background)
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
        envelope=provider.error_envelope,
    )


def _transport_error(provider: Provider, exc: httpx.TransportError) -> UpstreamError:
    return UpstreamError(
        f"Could not reach the upstream provider {provider.name!r}.",
        context={"provider": provider.name, "cause": type(exc).__name__},
        envelope=provider.error_envelope,
    )
