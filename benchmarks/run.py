"""Benchmark the transformation pipeline.

    python -m benchmarks.run
    python -m benchmarks.run --repeat 20 --json results.json

Byte and token counts are exactly reproducible: the corpora are generated from a
fixed seed and the transformers are deterministic. Runtime is not reproducible —
it is reported as the median of ``--repeat`` runs, with the minimum alongside,
because the minimum is the number least polluted by whatever else the machine was
doing.

The token counter in use is printed, and it matters. Without tiktoken's encoding
files the heuristic counter is used: reduction *percentages* stay trustworthy,
because both sides are measured the same way, but absolute token counts do not.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.datasets import Dataset, all_datasets
from gateway.config import Settings
from gateway.optimizers import TransformationRequest, build_pipeline
from gateway.tokenizers import TokenCounterFactory


@dataclass(frozen=True, slots=True)
class Result:
    dataset: str
    description: str
    content_type: str
    transformer: str
    original_bytes: int
    optimized_bytes: int
    byte_reduction_pct: float
    original_tokens: int
    optimized_tokens: int
    token_reduction_pct: float
    median_ms: float
    min_ms: float
    throughput_mb_s: float


def _chat_body(content: str) -> bytes:
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": content}]}
    return json.dumps(payload).encode("utf-8")


async def _benchmark(dataset: Dataset, repeat: int) -> Result:
    settings = Settings(_env_file=None)
    counters = TokenCounterFactory.from_settings(settings)
    pipeline = build_pipeline(settings, counters)

    body = _chat_body(dataset.content)
    request = TransformationRequest("POST", "chat/completions", "application/json", body)

    # Warm up: first call pays for lxml's parser tables and any encoding load.
    report = await pipeline.transform(request)

    timings: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        await pipeline.transform(request)
        timings.append((time.perf_counter() - started) * 1000)

    if not report.results:
        raise SystemExit(f"{dataset.name}: nothing was optimized ({report.skip_reason})")

    (segment,) = report.results
    median = statistics.median(timings)
    seconds = median / 1000
    throughput = (segment.original_size_bytes / 1_048_576) / seconds if seconds else 0.0

    return Result(
        dataset=dataset.name,
        description=dataset.description,
        content_type=segment.detected_content_type.value,
        transformer=segment.transformation_name,
        original_bytes=segment.original_size_bytes,
        optimized_bytes=segment.transformed_size_bytes,
        byte_reduction_pct=segment.byte_reduction_pct,
        original_tokens=segment.original_token_count,
        optimized_tokens=segment.transformed_token_count,
        token_reduction_pct=segment.token_reduction_pct,
        median_ms=round(median, 3),
        min_ms=round(min(timings), 3),
        throughput_mb_s=round(throughput, 1),
    )


def _print_table(results: list[Result]) -> None:
    header = (
        f"{'dataset':<20} {'type':<6} {'bytes in':>10} {'bytes out':>10} {'saved':>7} "
        f"{'tok in':>8} {'tok out':>8} {'saved':>7} {'median':>9} {'MB/s':>7}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result.dataset:<20} {result.content_type:<6} "
            f"{result.original_bytes:>10,} {result.optimized_bytes:>10,} "
            f"{result.byte_reduction_pct:>6.1f}% "
            f"{result.original_tokens:>8,} {result.optimized_tokens:>8,} "
            f"{result.token_reduction_pct:>6.1f}% "
            f"{result.median_ms:>8.2f}ms {result.throughput_mb_s:>7.1f}"
        )

    total_before = sum(result.original_tokens for result in results)
    total_after = sum(result.optimized_tokens for result in results)
    saved = total_before - total_after
    print("-" * len(header))
    print(
        f"{'TOTAL':<20} {'':<6} "
        f"{sum(r.original_bytes for r in results):>10,} "
        f"{sum(r.optimized_bytes for r in results):>10,} "
        f"{'':>7} {total_before:>8,} {total_after:>8,} "
        f"{saved / total_before * 100:>6.1f}%"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=10, help="timed runs per dataset")
    parser.add_argument("--json", type=Path, help="also write results as JSON")
    args = parser.parse_args()

    settings = Settings(_env_file=None)
    counter = TokenCounterFactory.from_settings(settings).for_model("gpt-4o-mini")

    print(f"token counter: {counter.name} (exact={counter.exact})")
    if not counter.exact:
        print(
            "  note: tiktoken encodings unavailable. Reduction percentages are sound;\n"
            "        absolute token counts are approximate.",
            file=sys.stderr,
        )
    print(f"timing: median of {args.repeat} runs\n")

    results = [asyncio.run(_benchmark(dataset, args.repeat)) for dataset in all_datasets()]
    _print_table(results)

    if args.json:
        payload = {
            "token_counter": counter.name,
            "exact_tokens": counter.exact,
            "repeat": args.repeat,
            "results": [asdict(result) for result in results],
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
