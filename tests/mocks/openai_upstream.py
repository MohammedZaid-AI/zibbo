"""A mock OpenAI-compatible upstream.

Bodies are byte literals, never re-serialized, so a test asserting
``gateway_response.content == CHAT_COMPLETION_BODY`` proves the gateway did not
touch a single byte — including the irregular whitespace below, which a
round-trip through ``json.dumps`` would silently normalize away.

The recorder captures exactly what the upstream saw, which is how the header
forwarding policy gets tested from the far side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request

# Deliberately ugly formatting: mixed spacing, non-alphabetical keys. Any
# re-serialization by the gateway changes these bytes and fails the test.
CHAT_COMPLETION_BODY = (
    b'{"id": "chatcmpl-abc123", "object":"chat.completion", "created": 1700000000,'
    b'  "model": "gpt-4o-mini-2024-07-18",'
    b' "choices": [{"index": 0, "message": {"role": "assistant",'
    b' "content": "Hello there."}, "finish_reason": "stop", "logprobs": null}],'
    b' "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},'
    b' "system_fingerprint": "fp_44709d6fcb"}'
)

MODELS_BODY = (
    b'{"object": "list", "data": [{"id": "gpt-4o-mini", "object": "model",'
    b' "created": 1700000000, "owned_by": "system"}]}'
)

_CHUNK_HEAD = (
    b'{"id": "chatcmpl-abc123", "object": "chat.completion.chunk",'
    b' "created": 1700000000, "model": "gpt-4o-mini-2024-07-18", "choices": [{"index": 0,'
)

SSE_CHUNKS: tuple[bytes, ...] = (
    b"data: " + _CHUNK_HEAD + b' "delta": {"role": "assistant", "content": ""},'
    b' "finish_reason": null}]}\n\n',
    b"data: " + _CHUNK_HEAD + b' "delta": {"content": "Hello"}, "finish_reason": null}]}\n\n',
    b"data: " + _CHUNK_HEAD + b' "delta": {"content": " there."}, "finish_reason": null}]}\n\n',
    b"data: " + _CHUNK_HEAD + b' "delta": {}, "finish_reason": "stop"}]}\n\n',
    b"data: [DONE]\n\n",
)
SSE_BODY = b"".join(SSE_CHUNKS)

# Headers an SDK relies on for retries and diagnostics. All must survive the hop.
RESPONSE_HEADERS = {
    "x-request-id": "upstream-req-9f8e7d",
    "openai-organization": "org-testing",
    "openai-processing-ms": "243",
    "openai-version": "2020-10-01",
    "x-ratelimit-limit-requests": "10000",
    "x-ratelimit-remaining-requests": "9999",
    "x-ratelimit-reset-requests": "6ms",
    "x-ratelimit-limit-tokens": "30000",
    "x-ratelimit-remaining-tokens": "29979",
}

# Models that make the mock misbehave in a specific, useful way.
MODEL_BAD_REQUEST = "trigger-400"
MODEL_RATE_LIMITED = "trigger-429"
MODEL_SERVER_ERROR = "trigger-500"

ERROR_BODY_400 = (
    b'{"error": {"message": "Invalid value for \'temperature\'", "type":'
    b' "invalid_request_error", "param": "temperature", "code": null}}'
)
ERROR_BODY_429 = (
    b'{"error": {"message": "Rate limit reached", "type": "rate_limit_error",'
    b' "param": null, "code": "rate_limit_exceeded"}}'
)


@dataclass(slots=True)
class RecordedRequest:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    body: bytes
    raw_headers: list[tuple[str, str]]
    """Every header occurrence. ``headers`` collapses repeats; this does not."""

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)  # type: ignore[no-any-return]

    def header_values(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.raw_headers if key.lower() == lowered]


@dataclass(slots=True)
class UpstreamRecorder:
    """Everything the upstream received, in order."""

    requests: list[RecordedRequest] = field(default_factory=list)

    @property
    def last(self) -> RecordedRequest:
        if not self.requests:
            raise AssertionError("the upstream received no requests")
        return self.requests[-1]

    @property
    def count(self) -> int:
        return len(self.requests)


async def _record(recorder: UpstreamRecorder, request: Request) -> bytes:
    body = await request.body()
    recorder.requests.append(
        RecordedRequest(
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            headers=dict(request.headers),
            body=body,
            raw_headers=[
                (key.decode("latin-1"), value.decode("latin-1"))
                for key, value in request.headers.raw
            ],
        )
    )
    return body


async def _sse() -> AsyncIterator[bytes]:
    for chunk in SSE_CHUNKS:
        yield chunk


def _error(status_code: int, body: bytes, extra: dict[str, str] | None = None) -> Response:
    headers = {**RESPONSE_HEADERS, **(extra or {})}
    return Response(body, status_code=status_code, media_type="application/json", headers=headers)


def create_upstream_app(recorder: UpstreamRecorder) -> Starlette:
    """An ASGI app that impersonates ``https://api.openai.com/v1``."""

    async def chat_completions(request: Request) -> Response:
        body = await _record(recorder, request)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, b'{"error": {"message": "invalid JSON"}}')

        model = payload.get("model")
        if model == MODEL_BAD_REQUEST:
            return _error(400, ERROR_BODY_400)
        if model == MODEL_RATE_LIMITED:
            return _error(
                429,
                ERROR_BODY_429,
                {
                    "retry-after": "20",
                    "x-should-retry": "true",
                    "x-ratelimit-remaining-requests": "0",
                },
            )
        if model == MODEL_SERVER_ERROR:
            return _error(500, b'{"error": {"message": "server had an error"}}')

        if payload.get("stream") is True:
            return StreamingResponse(
                _sse(),
                media_type="text/event-stream",
                headers={**RESPONSE_HEADERS, "cache-control": "no-cache"},
            )

        return Response(
            CHAT_COMPLETION_BODY,
            media_type="application/json",
            headers=RESPONSE_HEADERS,
        )

    async def models(request: Request) -> Response:
        await _record(recorder, request)
        return Response(MODELS_BODY, media_type="application/json", headers=RESPONSE_HEADERS)

    async def echo(request: Request) -> Response:
        """Catch-all: proves unmodelled endpoints are proxied too."""
        body = await _record(recorder, request)
        return Response(
            body or b'{"ok": true}',
            media_type=request.headers.get("content-type", "application/json"),
            headers=RESPONSE_HEADERS,
        )

    return Starlette(
        routes=[
            Route("/v1/chat/completions", chat_completions, methods=["POST"]),
            Route("/v1/models", models, methods=["GET"]),
            Route("/v1/{path:path}", echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ]
    )
