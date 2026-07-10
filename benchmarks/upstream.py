"""A minimal, fast OpenAI-compatible upstream for benchmarking.

Deliberately trivial: it must never be the bottleneck, or the measurement is of
this file rather than of the gateway. No parsing, no logging, a constant body.

    uvicorn benchmarks.upstream:app --port 8124 --no-access-log
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from starlette.requests import Request

CHAT_COMPLETION = (
    b'{"id":"chatcmpl-bench","object":"chat.completion","created":1700000000,'
    b'"model":"gpt-4o-mini","choices":[{"index":0,"message":{"role":"assistant",'
    b'"content":"ok"},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":9,"completion_tokens":1,"total_tokens":10}}'
)

HEADERS = {
    "x-request-id": "upstream-bench",
    "x-ratelimit-remaining-requests": "9999",
}


async def _sse(chunks: int, delay: float) -> AsyncIterator[bytes]:
    for index in range(chunks):
        yield b'data: {"choices":[{"delta":{"content":"tok%d"}}]}\n\n' % index
        if delay:
            await asyncio.sleep(delay)
    yield b"data: [DONE]\n\n"


async def chat_completions(request: Request) -> Response:
    body = await request.body()
    # Cheapest possible check; avoids a full JSON parse on the hot path.
    if b'"stream":true' in body.replace(b" ", b"") or b'"stream": true' in body:
        return StreamingResponse(
            _sse(int(request.query_params.get("chunks", 8)), 0.0),
            media_type="text/event-stream",
            headers=HEADERS,
        )
    return Response(CHAT_COMPLETION, media_type="application/json", headers=HEADERS)


async def catch_all(request: Request) -> Response:
    await request.body()
    return Response(b'{"ok":true}', media_type="application/json", headers=HEADERS)


app = Starlette(
    routes=[
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/{path:path}", catch_all, methods=["GET", "POST"]),
    ]
)
