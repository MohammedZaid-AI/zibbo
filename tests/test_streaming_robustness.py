"""Streaming under adverse conditions.

Every test here asks the same question in a different way: **is the upstream
connection released?** A proxy that leaks one connection per abandoned stream
starves its pool and stops serving, and it does so slowly enough that the cause is
never obvious from the symptom.

Driven against ``ProxyService`` with a controllable transport rather than through
the ASGI client, because httpx's ``ASGITransport`` concatenates response bodies and
would hide the very behaviour under test.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from httpx import AsyncClient
from starlette.responses import StreamingResponse

from gateway.errors import UpstreamError, UpstreamTimeoutError
from gateway.providers import OpenAIProvider, ProxyService

pytestmark = pytest.mark.integration

UPSTREAM = "http://upstream.test/v1"
STREAM_BODY = b'{"model": "gpt-4o-mini", "messages": [], "stream": true}'

CHUNKS = [
    b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n',
    b'data: {"choices":[{"delta":{"content":"two"}}]}\n\n',
    b"data: [DONE]\n\n",
]


class TrackedStream(httpx.AsyncByteStream):
    """An upstream body that records whether it was closed, and can fail on demand."""

    def __init__(
        self,
        chunks: list[bytes],
        *,
        fail_after: int | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._chunks = chunks
        self._fail_after = fail_after
        self._error = error or httpx.ReadError("connection reset by peer")
        self._delay = delay
        self.produced: list[bytes] = []
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for index, chunk in enumerate(self._chunks):
            if self._fail_after is not None and index >= self._fail_after:
                raise self._error
            if self._delay:
                await asyncio.sleep(self._delay)
            self.produced.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _client(stream: TrackedStream, status: int = 200) -> AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"content-type": "text/event-stream"}, stream=stream)

    return AsyncClient(transport=httpx.MockTransport(handler))


async def _open_stream(stream: TrackedStream) -> tuple[StreamingResponse, AsyncClient]:
    client = _client(stream)
    response = await ProxyService(client).forward(
        provider=OpenAIProvider(base_url=UPSTREAM),
        method="POST",
        path="chat/completions",
        query="",
        headers={"content-type": "application/json"},
        body=STREAM_BODY,
    )
    assert isinstance(response, StreamingResponse)
    return response, client


# -- The happy path, for contrast ------------------------------------------


async def test_a_completed_stream_closes_the_upstream_response() -> None:
    stream = TrackedStream(list(CHUNKS))
    response, client = await _open_stream(stream)

    received = [chunk async for chunk in response.body_iterator]

    assert b"".join(received) == b"".join(CHUNKS)  # type: ignore[arg-type]
    assert stream.closed, "upstream response left open after a clean finish"
    await client.aclose()


# -- Client disconnects ----------------------------------------------------


async def test_client_disconnect_mid_stream_closes_the_upstream_response() -> None:
    """Starlette throws GeneratorExit into the body iterator. The connection must go back."""
    stream = TrackedStream(list(CHUNKS))
    response, client = await _open_stream(stream)

    iterator = response.body_iterator
    first = await anext(iterator)  # type: ignore[arg-type]
    assert first == CHUNKS[0]
    assert not stream.closed

    await iterator.aclose()  # type: ignore[union-attr]

    assert stream.closed, "upstream connection leaked when the caller hung up"
    assert stream.produced == [CHUNKS[0]], "upstream kept producing after the caller left"
    await client.aclose()


async def test_client_disconnect_before_the_first_chunk_still_closes() -> None:
    stream = TrackedStream(list(CHUNKS))
    response, client = await _open_stream(stream)

    await response.body_iterator.aclose()  # type: ignore[union-attr]

    assert stream.closed
    await client.aclose()


async def test_cancelling_the_consuming_task_closes_the_upstream_response() -> None:
    """A cancelled request task must not strand the upstream connection."""
    stream = TrackedStream(list(CHUNKS), delay=0.05)
    response, client = await _open_stream(stream)

    async def consume() -> None:
        async for _ in response.body_iterator:
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The generator's `finally` runs during cancellation cleanup.
    await asyncio.sleep(0)
    assert stream.closed
    await client.aclose()


async def test_no_tasks_are_left_hanging_after_a_stream() -> None:
    before = len(asyncio.all_tasks())
    stream = TrackedStream(list(CHUNKS))
    response, client = await _open_stream(stream)
    async for _ in response.body_iterator:
        pass
    await client.aclose()

    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) == before


# -- The upstream breaks mid-stream ----------------------------------------


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (httpx.ReadError("connection reset by peer"), "upstream_error"),
        (httpx.RemoteProtocolError("peer closed connection"), "upstream_error"),
        (httpx.ReadTimeout("no data"), "upstream_timeout"),
    ],
)
async def test_a_broken_stream_emits_an_sse_error_frame(
    error: Exception, expected_code: str
) -> None:
    """Silence would let the SDK hand back a truncated answer as if it were complete."""
    stream = TrackedStream(list(CHUNKS), fail_after=1, error=error)
    response, client = await _open_stream(stream)

    received = [chunk async for chunk in response.body_iterator]

    assert received[0] == CHUNKS[0], "chunks before the failure are still delivered"
    final = json.loads(received[-1].removeprefix(b"data: ").strip())  # type: ignore[union-attr]
    assert final["error"]["code"] == expected_code
    assert final["error"]["type"] == "upstream_error"
    assert stream.closed
    await client.aclose()


async def test_a_broken_stream_does_not_emit_done() -> None:
    """`[DONE]` would tell the SDK the response finished normally."""
    stream = TrackedStream(list(CHUNKS), fail_after=1)
    response, client = await _open_stream(stream)

    body = b"".join([chunk async for chunk in response.body_iterator])  # type: ignore[misc]

    assert b"[DONE]" not in body
    await client.aclose()


async def test_a_stream_that_fails_on_its_very_first_chunk_still_signals() -> None:
    stream = TrackedStream(list(CHUNKS), fail_after=0)
    response, client = await _open_stream(stream)

    received = [chunk async for chunk in response.body_iterator]

    assert len(received) == 1
    assert b'"error"' in received[0]  # type: ignore[operator]
    assert stream.closed
    await client.aclose()


# -- Slow producers and consumers ------------------------------------------


async def test_a_slow_upstream_does_not_buffer(anyio_backend: object = None) -> None:
    """Chunks reach the caller as they arrive, not when the stream ends."""
    stream = TrackedStream(list(CHUNKS), delay=0.05)
    response, client = await _open_stream(stream)

    loop = asyncio.get_running_loop()
    started = loop.time()
    iterator = response.body_iterator
    await anext(iterator)  # type: ignore[arg-type]
    first_at = loop.time() - started

    async for _ in iterator:
        pass
    total = loop.time() - started

    assert first_at < total / 2, "first chunk waited for the whole stream"
    await client.aclose()


async def test_a_slow_consumer_applies_back_pressure() -> None:
    """The upstream must not be drained ahead of a caller that reads slowly."""
    stream = TrackedStream(list(CHUNKS))
    response, client = await _open_stream(stream)

    iterator = response.body_iterator
    await anext(iterator)  # type: ignore[arg-type]
    await asyncio.sleep(0.05)

    assert stream.produced == [CHUNKS[0]], "upstream ran ahead of the consumer"
    await iterator.aclose()  # type: ignore[union-attr]
    await client.aclose()


# -- Failures before the stream opens --------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("dns failure"), UpstreamError),
        (httpx.ConnectTimeout("timed out"), UpstreamTimeoutError),
        (httpx.ReadTimeout("timed out"), UpstreamTimeoutError),
    ],
)
async def test_failure_before_headers_raises_a_gateway_error(
    error: Exception, expected: type[Exception]
) -> None:
    """Nothing has been sent yet, so a proper HTTP error is still possible."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    async with AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(expected):
            await ProxyService(client).forward(
                provider=OpenAIProvider(base_url=UPSTREAM),
                method="POST",
                path="chat/completions",
                query="",
                headers={"content-type": "application/json"},
                body=STREAM_BODY,
            )


# -- Stale keepalive connection: the intermittent-502 case -----------------


def _counting_client(
    responses: list[Exception | httpx.Response],
) -> tuple[AsyncClient, list[int]]:
    """A client whose handler yields ``responses`` in order, counting each send."""
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        index = calls[0]
        calls[0] += 1
        outcome = responses[min(index, len(responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return AsyncClient(transport=httpx.MockTransport(handler)), calls


async def _forward_stream(client: AsyncClient) -> StreamingResponse:
    response = await ProxyService(client).forward(
        provider=OpenAIProvider(base_url=UPSTREAM),
        method="POST",
        path="chat/completions",
        query="",
        headers={"content-type": "application/json"},
        body=STREAM_BODY,
    )
    assert isinstance(response, StreamingResponse)
    return response


async def test_a_dropped_keepalive_connection_is_retried_and_succeeds() -> None:
    """The exact intermittent 502: a pooled socket the server had already closed.

    httpx raises before a byte is sent, so re-sending cannot double-submit. The
    caller must see the real 200 stream, not a gateway 502.
    """
    stream = TrackedStream(list(CHUNKS))
    ok = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    client, calls = _counting_client(
        [httpx.RemoteProtocolError("Server disconnected without sending a response"), ok]
    )

    response = await _forward_stream(client)
    received = [chunk async for chunk in response.body_iterator]

    assert b"".join(received) == b"".join(CHUNKS)  # type: ignore[arg-type]
    assert calls[0] == 2, "the send should have been retried exactly once"
    await client.aclose()


async def test_reconnect_attempts_are_bounded() -> None:
    """A provider that is genuinely down still fails — retries do not loop forever."""
    client, calls = _counting_client([httpx.RemoteProtocolError("still gone")])

    with pytest.raises(UpstreamError):
        await _forward_stream(client)

    # default reconnect_attempts=1 → one original send plus one retry
    assert calls[0] == 2
    await client.aclose()


async def test_a_timeout_is_never_retried() -> None:
    """A slow server may still be processing; retrying could double-submit."""
    client, calls = _counting_client([httpx.ConnectTimeout("timed out")])

    with pytest.raises(UpstreamTimeoutError):
        await _forward_stream(client)

    assert calls[0] == 1, "timeouts must not be retried"
    await client.aclose()


async def test_partial_chunk_delivery_is_relayed_verbatim() -> None:
    """SSE framing may split across TCP reads. The gateway must not reframe."""
    halves = [b'data: {"choices":[{"delta":', b'{"content":"x"}}]}\n\n', b"data: [DONE]\n\n"]
    stream = TrackedStream(halves)
    response, client = await _open_stream(stream)

    received = [chunk async for chunk in response.body_iterator]

    assert received == halves, "chunk boundaries were altered"
    await client.aclose()
