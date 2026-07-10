"""Large-payload behaviour: latency, memory, CPU, and graceful degradation.

    python -m benchmarks.large_payload

Memory is measured with ``tracemalloc``, which counts Python allocations rather
than RSS. That is the right instrument here: it attributes the allocation to the
transformation instead of to whatever the allocator happened to keep. The number
that matters is **peak memory as a multiple of input size** — a proxy that needs
20x the payload in RAM cannot be sized.

The 10 MB case is the interesting one. It exceeds the default
``optimization_max_body_bytes`` (8 MB), so the gateway forwards it untouched
rather than parsing it. That is the graceful degradation: too big to optimize
means proxied, never refused.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
import tracemalloc
from dataclasses import dataclass

from benchmarks.datasets import large_html
from gateway.config import Settings
from gateway.optimizers import TransformationRequest, build_pipeline
from gateway.tokenizers import TokenCounterFactory

TARGET_SIZES_MB = (1, 5, 10)
REPEAT = 3


@dataclass(frozen=True, slots=True)
class LargeResult:
    label: str
    input_mb: float
    optimized: bool
    skip_reason: str | None
    output_mb: float
    token_reduction_pct: float
    wall_ms: float
    cpu_ms: float
    peak_mib: float
    peak_ratio: float
    throughput_mb_s: float


def _html_of_size(target_bytes: int) -> str:
    return large_html(target_bytes)


def _chat_body(content: str) -> bytes:
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": content}]}
    return json.dumps(payload).encode("utf-8")


async def _measure(label: str, content: str, settings: Settings) -> LargeResult:
    pipeline = build_pipeline(settings, TokenCounterFactory.from_settings(settings))
    body = _chat_body(content)
    request = TransformationRequest("POST", "chat/completions", "application/json", body)

    report = await pipeline.transform(request)  # warm the parser tables

    wall: list[float] = []
    cpu: list[float] = []
    for _ in range(REPEAT):
        wall_start, cpu_start = time.perf_counter(), time.process_time()
        await pipeline.transform(request)
        wall.append((time.perf_counter() - wall_start) * 1000)
        cpu.append((time.process_time() - cpu_start) * 1000)

    tracemalloc.start()
    await pipeline.transform(request)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    input_mb = len(body) / 1_048_576
    median_wall = statistics.median(wall)
    seconds = median_wall / 1000

    return LargeResult(
        label=label,
        input_mb=round(input_mb, 2),
        optimized=report.applied,
        skip_reason=report.skip_reason.value if report.skip_reason else None,
        output_mb=round(len(report.body) / 1_048_576, 2),
        token_reduction_pct=report.token_reduction_pct,
        wall_ms=round(median_wall, 1),
        cpu_ms=round(statistics.median(cpu), 1),
        peak_mib=round(peak / 1_048_576, 1),
        peak_ratio=round(peak / max(len(body), 1), 2),
        throughput_mb_s=round(input_mb / seconds, 1) if seconds else 0.0,
    )


def _print(results: list[LargeResult]) -> None:
    header = (
        f"{'payload':<22} {'in MB':>6} {'out MB':>7} {'tok saved':>10} "
        f"{'wall':>9} {'cpu':>9} {'peak MiB':>9} {'peak/in':>8} {'MB/s':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        saved = f"{r.token_reduction_pct:.1f}%" if r.optimized else f"({r.skip_reason})"
        print(
            f"{r.label:<22} {r.input_mb:>6.2f} {r.output_mb:>7.2f} {saved:>10} "
            f"{r.wall_ms:>7.1f}ms {r.cpu_ms:>7.1f}ms {r.peak_mib:>9.1f} "
            f"{r.peak_ratio:>7.2f}x {r.throughput_mb_s:>6.1f}"
        )


async def main() -> None:
    default = Settings(_env_file=None)
    # A second configuration that lifts the cap, so the 10 MB case can be measured
    # doing the work as well as measured refusing it.
    uncapped = Settings(_env_file=None, optimization_max_body_bytes=64_000_000)

    results: list[LargeResult] = []
    for size_mb in TARGET_SIZES_MB:
        content = _html_of_size(size_mb * 1_048_576)
        results.append(await _measure(f"{size_mb} MB HTML", content, default))

    print("=== default settings (optimization_max_body_bytes = 8 MB) ===\n")
    _print(results)

    over_cap = await _measure("10 MB HTML (uncapped)", _html_of_size(10 * 1_048_576), uncapped)
    print("\n=== cap lifted, so the 10 MB payload is actually transformed ===\n")
    _print([over_cap])


if __name__ == "__main__":
    asyncio.run(main())
