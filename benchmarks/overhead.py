"""How much latency does the gateway add?

Measures the same request twice — straight to the provider, and through the
gateway to the provider — and reports the difference.

    # terminal 1
    uvicorn benchmarks.upstream:app --port 8124 --no-access-log
    # terminal 2
    ZIBBO_OPENAI_BASE_URL=http://127.0.0.1:8124/v1 \
      uvicorn gateway.main:app --port 8123 --no-access-log
    # terminal 3
    python -m benchmarks.overhead --requests 2000 --concurrency 16

Percentiles are reported because a mean hides the tail, and a proxy is judged on
its tail. Both legs run against the same upstream in the same process on the same
machine, so the difference is the gateway and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import dataclass

import httpx

CLEAN_REQUEST = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": "Summarize the meeting notes."}],
}

NOISY_HTML = (
    "<!DOCTYPE html><html><head><title>Doc</title><script>track()</script>"
    "<style>.a{color:red}</style></head><body><nav class='navbar'><a href='/'>Home</a></nav>"
    "<div class='cookie-consent'>Accept</div><div class='ad-slot'>BUY</div>"
    "<main><h1>Title</h1><p>Body   text   here.</p><ul><li>One</li><li>Two</li></ul></main>"
    "<footer>(c) 2026</footer></body></html>"
)
OPTIMIZED_REQUEST = {
    "model": "gpt-4o-mini",
    "messages": [{"role": "user", "content": NOISY_HTML}],
}


@dataclass(frozen=True, slots=True)
class Stats:
    label: str
    count: int
    errors: int
    rps: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    server_p50_ms: float | None = None
    """Median of the gateway's own `X-Process-Time`: time inside the process,
    including its call upstream. Subtracting the direct client latency isolates the
    gateway's CPU cost from the extra network hop it introduces."""

    @classmethod
    def of(
        cls,
        label: str,
        samples: list[float],
        errors: int,
        elapsed: float,
        server_times: list[float] | None = None,
    ) -> Stats:
        ordered = sorted(samples)
        quantiles = statistics.quantiles(ordered, n=100, method="inclusive")
        return cls(
            label=label,
            count=len(ordered),
            errors=errors,
            rps=round(len(ordered) / elapsed, 1) if elapsed else 0.0,
            mean_ms=round(statistics.fmean(ordered), 2),
            p50_ms=round(statistics.median(ordered), 2),
            p95_ms=round(quantiles[94], 2),
            p99_ms=round(quantiles[98], 2),
            max_ms=round(ordered[-1], 2),
            server_p50_ms=round(statistics.median(server_times), 2) if server_times else None,
        )


async def _worker(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, object],
    queue: asyncio.Queue[None],
    samples: list[float],
    errors: list[int],
    server_times: list[float],
) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        started = time.perf_counter()
        try:
            response = await client.post(url, json=payload)
            if response.status_code != 200:
                errors.append(1)
        except httpx.HTTPError:
            errors.append(1)
        else:
            samples.append((time.perf_counter() - started) * 1000)
            process_time = response.headers.get("x-process-time")
            if process_time:
                server_times.append(float(process_time))
        finally:
            queue.task_done()


async def _run(
    label: str, url: str, payload: dict[str, object], requests: int, concurrency: int
) -> Stats:
    limits = httpx.Limits(
        max_connections=concurrency * 2, max_keepalive_connections=concurrency * 2
    )
    async with httpx.AsyncClient(timeout=30.0, limits=limits) as client:
        # Warm connections, JIT-ish caches, and the tiktoken encoding.
        for _ in range(20):
            await client.post(url, json=payload)

        queue: asyncio.Queue[None] = asyncio.Queue()
        for _ in range(requests):
            queue.put_nowait(None)

        samples: list[float] = []
        errors: list[int] = []
        server_times: list[float] = []
        started = time.perf_counter()
        await asyncio.gather(
            *(
                _worker(client, url, payload, queue, samples, errors, server_times)
                for _ in range(concurrency)
            )
        )
        elapsed = time.perf_counter() - started

    return Stats.of(label, samples, len(errors), elapsed, server_times)


def _print(rows: list[Stats]) -> None:
    header = (
        f"{'scenario':<34} {'n':>6} {'err':>4} {'rps':>8} "
        f"{'mean':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'max':>8}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.label:<34} {row.count:>6} {row.errors:>4} {row.rps:>8.1f} "
            f"{row.mean_ms:>7.2f}m {row.p50_ms:>7.2f}m {row.p95_ms:>7.2f}m "
            f"{row.p99_ms:>7.2f}m {row.max_ms:>7.2f}m"
        )


def _overhead(direct: Stats, through: Stats) -> None:
    print(f"\n  added p50 (client-observed): {through.p50_ms - direct.p50_ms:+.2f} ms")
    print(f"  added p95 (client-observed): {through.p95_ms - direct.p95_ms:+.2f} ms")
    print(f"  added p99 (client-observed): {through.p99_ms - direct.p99_ms:+.2f} ms")
    if through.server_p50_ms is not None:
        # X-Process-Time already contains the gateway's own upstream round trip, so
        # what is left after subtracting the direct latency is the gateway's own work.
        in_process = through.server_p50_ms - direct.p50_ms
        hop = (through.p50_ms - direct.p50_ms) - in_process
        print(f"    of which gateway CPU:     {in_process:+.2f} ms")
        print(f"    of which extra network hop: {hop:+.2f} ms")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="http://127.0.0.1:8124/v1/chat/completions")
    parser.add_argument("--gateway", default="http://127.0.0.1:8123/v1/chat/completions")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--json", type=str, default="")
    args = parser.parse_args()

    print(f"{args.requests} requests, concurrency {args.concurrency}\n")

    rows = [
        await _run(
            "direct -> upstream (clean)",
            args.upstream,
            CLEAN_REQUEST,
            args.requests,
            args.concurrency,
        ),
        await _run(
            "gateway -> upstream (clean)",
            args.gateway,
            CLEAN_REQUEST,
            args.requests,
            args.concurrency,
        ),
        await _run(
            "direct -> upstream (html)",
            args.upstream,
            OPTIMIZED_REQUEST,
            args.requests,
            args.concurrency,
        ),
        await _run(
            "gateway -> upstream (html, optimized)",
            args.gateway,
            OPTIMIZED_REQUEST,
            args.requests,
            args.concurrency,
        ),
    ]
    _print(rows)

    print("\n=== gateway overhead, already-optimal request (pure proxy cost) ===")
    _overhead(rows[0], rows[1])
    print("\n=== gateway overhead, HTML request (proxy + transformation) ===")
    _overhead(rows[2], rows[3])

    if args.json:
        from dataclasses import asdict

        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump([asdict(row) for row in rows], handle, indent=2)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    asyncio.run(main())
