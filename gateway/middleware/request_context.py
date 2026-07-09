"""Request correlation, timing, and the access log.

Implemented as raw ASGI rather than ``BaseHTTPMiddleware``: the latter pumps the
response through an anyio memory stream, which breaks back-pressure for the
streamed provider responses added in Phase 8. Raw ASGI also lets us stamp
``X-Request-ID`` onto the response before the first byte leaves.

Contextvars are cleared on *entry* rather than reset on exit. An unhandled
exception propagates past this middleware up to Starlette's ``ServerErrorMiddleware``,
which is where our 500 handler runs — resetting on the way out would strip the
request id from precisely the log line that needs it most.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Final

import structlog
from starlette.datastructures import Headers, MutableHeaders

from gateway.logging import get_logger
from gateway.utils.ids import is_request_id, new_request_id

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = get_logger(__name__)

REQUEST_ID_HEADER: Final = "x-request-id"
GATEWAY_REQUEST_ID_HEADER: Final = "x-llmgateway-request-id"
PROCESS_TIME_HEADER: Final = "x-process-time"
OPTIMIZATION_HEADER: Final = "x-llmgateway-optimization"
TOKENS_SAVED_HEADER: Final = "x-llmgateway-tokens-saved"

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """Return the current request's id, or ``None`` outside a request."""
    return _request_id.get()


class RequestContextMiddleware:
    """Assigns a request id, binds log context, times the request, logs the access line."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        quiet_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        # Liveness probes fire every few seconds; they belong at DEBUG, not INFO.
        self.quiet_paths = quiet_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)
        method: str = scope["method"]
        path: str = scope.get("path", "")

        _request_id.set(request_id)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id, method=method, path=path)

        # Make the id reachable via `request.state.request_id` in route handlers.
        scope.setdefault("state", {})["request_id"] = request_id

        started = time.perf_counter()
        status_code = 500
        response_bytes = 0

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_bytes
            if message["type"] == "http.response.body":
                # Counted per chunk, so a streamed response reports its true size.
                response_bytes += len(message.get("body", b""))
            if message["type"] == "http.response.start":
                status_code = message["status"]
                elapsed_ms = (time.perf_counter() - started) * 1000
                headers = MutableHeaders(scope=message)

                # Our id, always, under a name nobody else claims.
                headers[GATEWAY_REQUEST_ID_HEADER] = request_id

                # `x-request-id` belongs to whoever already set it. A proxied
                # response carries the *provider's* id, which is what an SDK
                # reports in its exceptions and what provider support asks for.
                # Only when nothing upstream claimed it do we supply our own.
                # (Assignment, not append: the error handlers set it too, and a
                # duplicate would reach the client as "id, id".)
                if REQUEST_ID_HEADER not in headers:
                    headers[REQUEST_ID_HEADER] = request_id

                headers[PROCESS_TIME_HEADER] = f"{elapsed_ms:.2f}"
            await send(message)

        request_bytes = _declared_length(scope)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            # The 500 body is rendered upstream of us; here we only record the timing.
            logger.warning("request_failed", duration_ms=duration_ms, request_bytes=request_bytes)
            raise
        else:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._log_access(
                scope,
                status_code=status_code,
                duration_ms=duration_ms,
                path=path,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
            )

    @staticmethod
    def _resolve_request_id(scope: Scope) -> str:
        """Honour a caller-supplied id so traces span the client and the gateway."""
        inbound = Headers(scope=scope).get(REQUEST_ID_HEADER)
        if inbound and is_request_id(inbound):
            return inbound
        return new_request_id()

    def _log_access(
        self,
        scope: Scope,
        *,
        status_code: int,
        duration_ms: float,
        path: str,
        request_bytes: int,
        response_bytes: int,
    ) -> None:
        client = scope.get("client")
        event = logger.debug if path in self.quiet_paths else logger.info
        event(
            "request_completed",
            status_code=status_code,
            duration_ms=duration_ms,
            request_bytes=request_bytes,
            response_bytes=response_bytes,
            client_ip=client[0] if client else None,
        )


def _declared_length(scope: Scope) -> int:
    """Request size from ``Content-Length``. Zero for chunked or bodiless requests."""
    raw = Headers(scope=scope).get("content-length")
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        return 0
