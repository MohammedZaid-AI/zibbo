"""Value types for the coding benchmark suite.

A ``BenchmarkCase`` is one dataset entry (a realistic request). A ``CaseResult`` is what
the pipeline did to it, for one provider's tokenizer. A ``SuiteResult`` is the whole run
plus the aggregates the website and README consume.

Everything here is metadata about sizes and transformations — no case carries the dataset
content into a result, so a result is always safe to publish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One dataset entry: a realistic request an assistant would send."""

    id: str
    project: str
    scenario: str
    file: str
    media_type: str
    description: str


@dataclass(frozen=True, slots=True)
class CaseResult:
    """What Zibbo did to one case, counted with one provider's tokenizer."""

    case_id: str
    project: str
    scenario: str
    content_type: str
    original_bytes: int
    optimized_bytes: int
    original_tokens: int
    optimized_tokens: int
    transformers: tuple[str, ...]
    cache_hit: bool
    transformation_ms: float

    @property
    def tokens_saved(self) -> int:
        return self.original_tokens - self.optimized_tokens

    @property
    def bytes_removed(self) -> int:
        return self.original_bytes - self.optimized_bytes

    @property
    def token_reduction_pct(self) -> float:
        return (
            round(self.tokens_saved / self.original_tokens * 100, 2)
            if self.original_tokens
            else 0.0
        )

    @property
    def helped(self) -> bool:
        return bool(self.transformers) and self.tokens_saved > 0


@dataclass(frozen=True, slots=True)
class TransformerCount:
    name: str
    count: int


@dataclass(frozen=True, slots=True)
class FileTypeStat:
    content_type: str
    cases: int
    avg_token_reduction_pct: float


@dataclass(frozen=True, slots=True)
class SuiteResult:
    """A full run for one provider, with the aggregates the assets are built from."""

    provider_key: str
    provider_label: str
    model: str
    usd_per_million_input_tokens: float
    cases: tuple[CaseResult, ...] = field(default_factory=tuple)

    # -- totals ----------------------------------------------------------

    @property
    def total_original_tokens(self) -> int:
        return sum(c.original_tokens for c in self.cases)

    @property
    def total_optimized_tokens(self) -> int:
        return sum(c.optimized_tokens for c in self.cases)

    @property
    def total_tokens_saved(self) -> int:
        return self.total_original_tokens - self.total_optimized_tokens

    @property
    def total_original_bytes(self) -> int:
        return sum(c.original_bytes for c in self.cases)

    @property
    def total_optimized_bytes(self) -> int:
        return sum(c.optimized_bytes for c in self.cases)

    # -- averages (the website numbers) ----------------------------------

    @property
    def avg_token_reduction_pct(self) -> float:
        return round(mean(c.token_reduction_pct for c in self.cases), 2) if self.cases else 0.0

    @property
    def overall_token_reduction_pct(self) -> float:
        total = self.total_original_tokens
        return round(self.total_tokens_saved / total * 100, 2) if total else 0.0

    @property
    def avg_transformation_ms(self) -> float:
        return round(mean(c.transformation_ms for c in self.cases), 3) if self.cases else 0.0

    @property
    def cache_hit_rate(self) -> float:
        return (
            round(mean(1.0 if c.cache_hit else 0.0 for c in self.cases), 4) if self.cases else 0.0
        )

    @property
    def cost_before_usd(self) -> float:
        return round(self.total_original_tokens / 1_000_000 * self.usd_per_million_input_tokens, 6)

    @property
    def cost_after_usd(self) -> float:
        return round(self.total_optimized_tokens / 1_000_000 * self.usd_per_million_input_tokens, 6)

    @property
    def cost_saved_usd(self) -> float:
        return round(self.cost_before_usd - self.cost_after_usd, 6)

    @property
    def cost_reduction_pct(self) -> float:
        before = self.cost_before_usd
        return round(self.cost_saved_usd / before * 100, 2) if before else 0.0

    @property
    def cases_helped(self) -> int:
        return sum(1 for c in self.cases if c.helped)

    def top_transformers(self, limit: int = 8) -> list[TransformerCount]:
        counts: dict[str, int] = {}
        for case in self.cases:
            for transformer in case.transformers:
                counts[transformer] = counts.get(transformer, 0) + 1
        ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [TransformerCount(name, count) for name, count in ordered[:limit]]

    def file_type_stats(self) -> list[FileTypeStat]:
        by_type: dict[str, list[CaseResult]] = {}
        for case in self.cases:
            by_type.setdefault(case.content_type, []).append(case)
        stats = [
            FileTypeStat(
                content_type=content_type,
                cases=len(group),
                avg_token_reduction_pct=round(mean(c.token_reduction_pct for c in group), 2),
            )
            for content_type, group in by_type.items()
        ]
        # Most improved first, then alphabetical for a stable order.
        return sorted(stats, key=lambda s: (-s.avg_token_reduction_pct, s.content_type))
