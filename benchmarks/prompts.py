"""Benchmark the deterministic prompt optimizer.

    python -m benchmarks.prompts
    python -m benchmarks.prompts --repeat 50 --json prompts.json

Runs realistic Claude Code prompts — repeated coding instructions, repeated
constraints, duplicate Project rules / Context sections, a preserved code block and
stack trace — through the *real* pipeline with prompt optimization enabled, and reports
bytes and tokens before/after, latency, and cache behaviour.

Everything is reproducible: the corpora are generated from a fixed seed and the
transformer is deterministic. Runtime is the median of ``--repeat`` runs. Cache hits are
demonstrated by running each prompt a second time through the same pipeline: the second
pass is served from the transformation cache, so it does no work.

No claim is made without a measurement. The token counter in use is printed; without
tiktoken's encoding files the heuristic counter is used, which keeps reduction
percentages sound but makes absolute token counts approximate.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from benchmarks.datasets import Dataset, prompt_datasets
from gateway.cache import build_transformation_cache
from gateway.config import Settings
from gateway.optimizers import build_pipeline
from gateway.tokenizers import TokenCounterFactory


@dataclass(frozen=True, slots=True)
class Result:
    dataset: str
    description: str
    content_type: str
    transformer: str
    steps: tuple[str, ...]
    original_bytes: int
    optimized_bytes: int
    byte_reduction_pct: float
    original_tokens: int
    optimized_tokens: int
    token_reduction_pct: float
    median_ms: float
    min_ms: float
    cache_hit_second_run: bool


def _pipeline() -> object:
    # Enable prompt optimization and disable the cross-request cache-warm surprise by
    # using a fresh in-memory cache each run — but keep it *on*, so the second pass can
    # demonstrate a hit.
    settings = Settings(
        _env_file=None,
        prompt_optimization_enabled=True,
        cache_enabled=True,
    )
    counters = TokenCounterFactory.from_settings(settings)
    cache = build_transformation_cache(settings)
    return build_pipeline(settings, counters, cache=cache)


def _benchmark(pipeline: object, dataset: Dataset, repeat: int) -> Result:
    # First pass: cold. Exercises detection, transform, token counting, and a cache put.
    cold = pipeline.preview(dataset.content)  # type: ignore[attr-defined]
    # Second pass: identical content, so it must come from the transformation cache.
    warm = pipeline.preview(dataset.content)  # type: ignore[attr-defined]

    timings: list[float] = []
    for _ in range(repeat):
        started = time.perf_counter()
        pipeline.preview(dataset.content)  # type: ignore[attr-defined]
        timings.append((time.perf_counter() - started) * 1000)

    median = statistics.median(timings)
    return Result(
        dataset=dataset.name,
        description=dataset.description,
        content_type=cold.detected_content_type.value,
        transformer=cold.transformation_name,
        steps=cold.transformations_applied,
        original_bytes=cold.original_size_bytes,
        optimized_bytes=cold.transformed_size_bytes,
        byte_reduction_pct=cold.byte_reduction_pct,
        original_tokens=cold.original_token_count,
        optimized_tokens=cold.transformed_token_count,
        token_reduction_pct=cold.token_reduction_pct,
        median_ms=round(median, 3),
        min_ms=round(min(timings), 3),
        cache_hit_second_run=warm.cache_hit,
    )


def _print_table(results: list[Result]) -> None:
    header = (
        f"{'dataset':<34} {'type':<7} {'bytes in':>9} {'bytes out':>9} {'saved':>7} "
        f"{'tok in':>7} {'tok out':>7} {'saved':>7} {'median':>9} {'cache':>6}"
    )
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.dataset:<34} {r.content_type:<7} "
            f"{r.original_bytes:>9,} {r.optimized_bytes:>9,} {r.byte_reduction_pct:>6.1f}% "
            f"{r.original_tokens:>7,} {r.optimized_tokens:>7,} {r.token_reduction_pct:>6.1f}% "
            f"{r.median_ms:>8.3f}ms {('hit' if r.cache_hit_second_run else 'miss'):>6}"
        )

    before = sum(r.original_tokens for r in results)
    after = sum(r.optimized_tokens for r in results)
    saved = before - after
    print("-" * len(header))
    pct = saved / before * 100 if before else 0.0
    print(
        f"{'TOTAL':<34} {'':<7} "
        f"{sum(r.original_bytes for r in results):>9,} "
        f"{sum(r.optimized_bytes for r in results):>9,} {'':>7} "
        f"{before:>7,} {after:>7,} {pct:>6.1f}%"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=20, help="timed runs per dataset")
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
    print(f"timing: median of {args.repeat} runs; cache column is the second-run outcome\n")

    pipeline = _pipeline()
    results = [_benchmark(pipeline, dataset, args.repeat) for dataset in prompt_datasets()]
    _print_table(results)

    if args.json:
        payload = {
            "token_counter": counter.name,
            "exact_tokens": counter.exact,
            "repeat": args.repeat,
            "results": [asdict(r) for r in results],
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
