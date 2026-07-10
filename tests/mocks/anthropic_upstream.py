"""A mock Anthropic Messages upstream.

Byte-literal bodies, like the OpenAI mock, so a test asserting equality proves the
gateway relayed the response untouched. The stream uses Anthropic's typed SSE events
(``message_start`` … ``message_stop``), which the gateway must pass through verbatim.
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

# Deliberately irregular spacing: a round-trip through json.dumps would change it.
MESSAGE_BODY = (
    b'{"id": "msg_bench01", "type":"message", "role": "assistant",'
    b' "model":"claude-sonnet-5",'
    b' "content": [{"type": "text", "text": "Hello there."}],'
    b' "stop_reason": "end_turn", "stop_sequence": null,'
    b' "usage": {"input_tokens": 12, "output_tokens": 3}}'
)

# Anthropic's stream is a sequence of typed events, each an SSE `event:`/`data:` pair.
SSE_EVENTS: tuple[bytes, ...] = (
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_bench01",'
    b'"role":"assistant","model":"claude-sonnet-5","content":[],"usage":{"input_tokens":12,'
    b'"output_tokens":0}}}\n\n',
    b'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    b'"content_block":{"type":"text","text":""}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":"Hello"}}\n\n',
    b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    b'"delta":{"type":"text_delta","text":" there."}}\n\n',
    b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
    b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
    b'"usage":{"output_tokens":3}}\n\n',
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
)
SSE_BODY = b"".join(SSE_EVENTS)

RESPONSE_HEADERS = {
    "request-id": "req_anthropic_upstream_01",
    "anthropic-ratelimit-requests-remaining": "4999",
    "anthropic-ratelimit-tokens-remaining": "39980",
}

MODEL_BAD_REQUEST = "trigger-400"
MODEL_RATE_LIMITED = "trigger-429"
MODEL_OVERLOADED = "trigger-529"

ERROR_BODY_400 = (
    b'{"type": "error", "error": {"type": "invalid_request_error",'
    b' "message": "max_tokens is required"}}'
)
ERROR_BODY_429 = (
    b'{"type": "error", "error": {"type": "rate_limit_error",'
    b' "message": "Number of requests has exceeded your rate limit"}}'
)


@dataclass(slots=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    raw_headers: list[tuple[str, str]]

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)  # type: ignore[no-any-return]

    def header_values(self, name: str) -> list[str]:
        lowered = name.lower()
        return [value for key, value in self.raw_headers if key.lower() == lowered]


@dataclass(slots=True)
class UpstreamRecorder:
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
    for event in SSE_EVENTS:
        yield event


def _error(status_code: int, body: bytes, extra: dict[str, str] | None = None) -> Response:
    headers = {**RESPONSE_HEADERS, **(extra or {})}
    return Response(body, status_code=status_code, media_type="application/json", headers=headers)


def create_upstream_app(recorder: UpstreamRecorder) -> Starlette:
    """An ASGI app that impersonates ``https://api.anthropic.com/v1``."""

    async def messages(request: Request) -> Response:
        body = await _record(recorder, request)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _error(400, b'{"type": "error", "error": {"message": "invalid JSON"}}')

        model = payload.get("model")
        if model == MODEL_BAD_REQUEST:
            return _error(400, ERROR_BODY_400)
        if model == MODEL_RATE_LIMITED:
            return _error(429, ERROR_BODY_429, {"retry-after": "30"})
        if model == MODEL_OVERLOADED:
            body = (
                b'{"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}'
            )
            return _error(529, body)

        if payload.get("stream") is True:
            return StreamingResponse(
                _sse(),
                media_type="text/event-stream",
                headers={**RESPONSE_HEADERS, "cache-control": "no-cache"},
            )
        return Response(MESSAGE_BODY, media_type="application/json", headers=RESPONSE_HEADERS)

    async def echo(request: Request) -> Response:
        body = await _record(recorder, request)
        return Response(
            body or b'{"ok": true}',
            media_type=request.headers.get("content-type", "application/json"),
            headers=RESPONSE_HEADERS,
        )

    return Starlette(
        routes=[
            Route("/v1/messages", messages, methods=["POST"]),
            Route("/v1/{path:path}", echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ]
    )
