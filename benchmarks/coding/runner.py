"""Run the dataset through the real pipeline, in-process, no network.

For each case: read the file, run it through Zibbo's pipeline once (cold — the real cost of
a first request) and once more (warm — proving a repeat is served from cache). Record the
cold reduction, the transformers applied, and whether a repeat hits the cache.

No provider is contacted. "WITHOUT Zibbo" is the original token count; "WITH Zibbo" is the
optimized one. That is the honest comparison for token reduction and cost — the only part
of end-to-end latency that is not machine- and network-specific.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.coding.models import BenchmarkCase, CaseResult, SuiteResult
from benchmarks.coding.pricing import DEFAULT_PROVIDER, PROVIDERS, Provider
from gateway.cache import build_transformation_cache
from gateway.config import Environment, Settings
from gateway.documents import build_document_service
from gateway.optimizers import (
    ContentDetector,
    OptimizerOptions,
    build_pipeline,
    build_transformer_registry,
)
from gateway.tokenizers import TokenCounterFactory

if TYPE_CHECKING:
    from gateway.optimizers import TransformationPipeline

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
MANIFEST = DATASETS_DIR / "manifest.json"


def load_cases(project: str | None = None) -> list[BenchmarkCase]:
    """Read the dataset manifest, optionally filtered to one project."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    cases = [
        BenchmarkCase(
            id=entry["id"],
            project=entry["project"],
            scenario=entry["scenario"],
            file=entry["file"],
            media_type=entry["media_type"],
            description=entry["description"],
        )
        for entry in manifest["cases"]
    ]
    if project is not None:
        cases = [case for case in cases if case.project.lower() == project.lower()]
    return sorted(cases, key=lambda c: c.id)


def _build_pipeline(model: str) -> TransformationPipeline:
    """A pipeline configured exactly like production, with the cache on.

    Isolated from ``.env`` so a developer's local configuration cannot skew results.
    """
    settings = Settings(_env_file=None, environment=Environment.TEST)  # type: ignore[call-arg]
    options = OptimizerOptions.from_settings(settings)
    return build_pipeline(
        settings,
        TokenCounterFactory.from_settings(settings),
        registry=build_transformer_registry(options),
        detector=ContentDetector(),
        document_service=build_document_service(settings),
        cache=build_transformation_cache(settings),
    )


def run_case(pipeline: TransformationPipeline, case: BenchmarkCase, model: str) -> CaseResult:
    """Measure one case: cold reduction plus whether a repeat hits the cache."""
    content = (DATASETS_DIR / case.file).read_text(encoding="utf-8")

    cold = pipeline.preview(content, model=model)
    warm = pipeline.preview(content, model=model)  # identical repeat -> should hit cache

    return CaseResult(
        case_id=case.id,
        project=case.project,
        scenario=case.scenario,
        content_type=cold.detected_content_type.value,
        original_bytes=cold.original_size_bytes,
        optimized_bytes=cold.transformed_size_bytes,
        original_tokens=cold.original_token_count,
        optimized_tokens=cold.transformed_token_count,
        transformers=cold.transformations_applied,
        cache_hit=warm.cache_hit,
        transformation_ms=cold.execution_time_ms,
    )


def run_suite(provider_key: str = DEFAULT_PROVIDER, *, project: str | None = None) -> SuiteResult:
    """Run every case (optionally one project) for one provider's tokenizer."""
    provider: Provider = PROVIDERS[provider_key]
    pipeline = _build_pipeline(provider.model)
    cases = load_cases(project)
    results = tuple(run_case(pipeline, case, provider.model) for case in cases)
    return SuiteResult(
        provider_key=provider.key,
        provider_label=provider.label,
        model=provider.model,
        usd_per_million_input_tokens=provider.usd_per_million_input_tokens,
        cases=results,
    )


def tokenizer_is_exact(model: str = "gpt-4o") -> bool:
    """Whether exact (tiktoken) counting is available here — reported for transparency."""
    return (
        TokenCounterFactory.from_settings(
            Settings(_env_file=None, environment=Environment.TEST)  # type: ignore[call-arg]
        )
        .for_model(model)
        .exact
    )
