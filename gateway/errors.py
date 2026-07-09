"""Exception hierarchy and the wire format for failures.

The gateway is a drop-in replacement for provider SDKs, so its error envelope
matches OpenAI's from day one::

    {"error": {"message": ..., "type": ..., "param": ..., "code": ..., "request_id": ...}}

Client SDKs parse this shape. Emitting anything else — FastAPI's default
``{"detail": ...}`` included — would break callers that only changed their base URL.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Any, Final

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gateway.logging import get_logger
from gateway.middleware.request_context import get_request_id

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = get_logger(__name__)

# Spelled out rather than taken from `status`, whose name for it (…_ENTITY vs
# …_CONTENT) has shifted across Starlette releases.
HTTP_422_UNPROCESSABLE_CONTENT: Final = 422


class ErrorType:
    """Canonical ``error.type`` values, mirroring the OpenAI taxonomy."""

    INVALID_REQUEST = "invalid_request_error"
    AUTHENTICATION = "authentication_error"
    PERMISSION = "permission_error"
    NOT_FOUND = "not_found_error"
    RATE_LIMIT = "rate_limit_error"
    API = "api_error"
    UPSTREAM = "upstream_error"
    SERVICE_UNAVAILABLE = "service_unavailable_error"


class GatewayError(Exception):
    """Base class for every failure the gateway raises deliberately.

    Anything not deriving from this is an unexpected bug and is reported as a
    generic ``api_error`` with the details withheld from the client.
    """

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type: str = ErrorType.API
    code: str | None = None

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        code: str | None = None,
        param: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.param = param
        self.context: dict[str, Any] = dict(context or {})
        if status_code is not None:
            self.status_code = status_code
        if error_type is not None:
            self.error_type = error_type
        if code is not None:
            self.code = code

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        return error_payload(
            message=self.message,
            error_type=self.error_type,
            code=self.code,
            param=self.param,
            request_id=request_id,
        )


class ConfigurationError(GatewayError):
    """The process is misconfigured. Surfaces at startup, never per-request."""

    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_type = ErrorType.API
    code = "configuration_error"


class BadRequestError(GatewayError):
    status_code = status.HTTP_400_BAD_REQUEST
    error_type = ErrorType.INVALID_REQUEST


class NotFoundError(GatewayError):
    status_code = status.HTTP_404_NOT_FOUND
    error_type = ErrorType.NOT_FOUND


class UpstreamError(GatewayError):
    """A provider returned an error or behaved unexpectedly."""

    status_code = status.HTTP_502_BAD_GATEWAY
    error_type = ErrorType.UPSTREAM
    code = "upstream_error"


class UpstreamTimeoutError(UpstreamError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "upstream_timeout"


class ServiceUnavailableError(GatewayError):
    """A dependency the gateway needs is down. Retryable."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_type = ErrorType.SERVICE_UNAVAILABLE
    code = "service_unavailable"


def error_payload(
    *,
    message: str,
    error_type: str,
    code: str | None = None,
    param: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Build the OpenAI-compatible error envelope."""
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
            "request_id": request_id,
        }
    }


def _json_error(
    status_code: int,
    payload: dict[str, Any],
    request_id: str | None,
) -> JSONResponse:
    headers = {"X-Request-ID": request_id} if request_id else None
    return JSONResponse(status_code=status_code, content=payload, headers=headers)


async def _handle_gateway_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, GatewayError)  # noqa: S101 — handler is registered per-type
    request_id = get_request_id()
    log = logger.bind(error_code=exc.code, error_type=exc.error_type, **exc.context)
    # 5xx means *we* broke; 4xx means the caller did. Only the former is our alarm.
    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        log.error("gateway_error", message=exc.message, exc_info=exc)
    else:
        log.info("client_error", message=exc.message)
    return _json_error(exc.status_code, exc.to_payload(request_id), request_id)


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    request_id = get_request_id()
    try:
        error_type = _HTTP_STATUS_TO_ERROR_TYPE[exc.status_code]
    except KeyError:
        error_type = (
            ErrorType.API
            if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR
            else ErrorType.INVALID_REQUEST
        )
    message = exc.detail if isinstance(exc.detail, str) else HTTPStatus(exc.status_code).phrase
    payload = error_payload(message=message, error_type=error_type, request_id=request_id)
    response = _json_error(exc.status_code, payload, request_id)
    if exc.headers:
        response.headers.update(exc.headers)
    return response


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    request_id = get_request_id()
    errors = exc.errors()
    first = errors[0] if errors else {}
    # ``loc`` is ("body", "messages", 0, "role"); the caller cares about the field path.
    location = [str(part) for part in first.get("loc", ()) if part not in ("body", "query")]
    param = ".".join(location) or None
    message = first.get("msg", "Request validation failed")

    logger.info("request_validation_failed", param=param, error_count=len(errors))
    payload = error_payload(
        message=message,
        error_type=ErrorType.INVALID_REQUEST,
        code="invalid_parameter",
        param=param,
        request_id=request_id,
    )
    payload["error"]["details"] = jsonable_encoder(errors)
    return _json_error(HTTP_422_UNPROCESSABLE_CONTENT, payload, request_id)


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Last line of defence. Never leaks internals to the caller."""
    request_id = get_request_id()
    logger.exception("unhandled_exception", exc_type=type(exc).__name__)
    payload = error_payload(
        message="An internal error occurred while processing the request.",
        error_type=ErrorType.API,
        code="internal_error",
        request_id=request_id,
    )
    return _json_error(status.HTTP_500_INTERNAL_SERVER_ERROR, payload, request_id)


_HTTP_STATUS_TO_ERROR_TYPE: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: ErrorType.INVALID_REQUEST,
    status.HTTP_401_UNAUTHORIZED: ErrorType.AUTHENTICATION,
    status.HTTP_403_FORBIDDEN: ErrorType.PERMISSION,
    status.HTTP_404_NOT_FOUND: ErrorType.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorType.INVALID_REQUEST,
    status.HTTP_429_TOO_MANY_REQUESTS: ErrorType.RATE_LIMIT,
    status.HTTP_502_BAD_GATEWAY: ErrorType.UPSTREAM,
    status.HTTP_503_SERVICE_UNAVAILABLE: ErrorType.SERVICE_UNAVAILABLE,
    status.HTTP_504_GATEWAY_TIMEOUT: ErrorType.UPSTREAM,
}


def register_exception_handlers(app: FastAPI) -> None:
    """Wire the handlers above onto the application."""
    app.add_exception_handler(GatewayError, _handle_gateway_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(Exception, _handle_unexpected_error)
