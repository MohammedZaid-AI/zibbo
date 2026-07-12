"""The in-memory analytics engine: aggregation, windows, and the report adapter."""

from __future__ import annotations

from gateway.analytics import AnalyticsEngine, event_from_report
from gateway.analytics.models import OptimizationEvent
from gateway.optimizers.models import (
    ContentType,
    SkipReason,
    TransformationReport,
    TransformationResult,
)


def _event(
    *,
    applied: bool = True,
    transformers: tuple[str, ...] = ("html",),
    tokens_before: int = 100,
    tokens_after: int = 30,
    cache_hits: int = 0,
    cache_lookups: int = 1,
    time_ms: float = 2.0,
) -> OptimizationEvent:
    return OptimizationEvent(
        timestamp=0.0,
        provider="openai",
        endpoint="chat/completions",
        applied=applied,
        skip_reason=None if applied else "content_already_optimal",
        content_types=("html",) if applied else (),
        transformers=transformers if applied else (),
        tokens_before=tokens_before if applied else 0,
        tokens_after=tokens_after if applied else 0,
        bytes_before=400 if applied else 0,
        bytes_after=120 if applied else 0,
        cache_hits=cache_hits,
        cache_lookups=cache_lookups,
        execution_time_ms=time_ms,
    )


def test_empty_engine_is_all_zero() -> None:
    engine = AnalyticsEngine()
    today = engine.today()
    assert today.requests == 0
    assert today.tokens_saved == 0
    assert today.token_reduction_pct == 0.0
    assert today.top_transformer is None
    assert engine.last_event is None


def test_aggregates_applied_and_skipped() -> None:
    engine = AnalyticsEngine()
    engine.record(_event(tokens_before=100, tokens_after=40))
    engine.record(_event(tokens_before=200, tokens_after=50))
    engine.record(_event(applied=False))

    stats = engine.all_time()
    assert stats.requests == 3
    assert stats.optimized == 2
    assert stats.skipped == 1
    assert stats.tokens_before == 300
    assert stats.tokens_after == 90
    assert stats.tokens_saved == 210
    assert stats.token_reduction_pct == 70.0


def test_top_transformer_is_the_biggest_saver() -> None:
    engine = AnalyticsEngine()
    engine.record(_event(transformers=("html",), tokens_before=100, tokens_after=90))  # saved 10
    engine.record(_event(transformers=("json",), tokens_before=100, tokens_after=20))  # saved 80
    top = engine.all_time().top_transformer
    assert top is not None
    assert top.name == "json"


def test_cache_hit_rate_and_latency_average() -> None:
    engine = AnalyticsEngine()
    engine.record(_event(cache_hits=1, cache_lookups=1, time_ms=2.0))
    engine.record(_event(cache_hits=0, cache_lookups=1, time_ms=4.0))
    stats = engine.all_time()
    assert stats.cache_hits == 1
    assert stats.cache_misses == 1
    assert stats.cache_hit_rate == 0.5
    assert stats.avg_latency_ms == 3.0


def test_recent_is_newest_first_and_bounded() -> None:
    engine = AnalyticsEngine(recent_capacity=3)
    for tokens in (10, 20, 30, 40):
        engine.record(_event(tokens_before=tokens, tokens_after=0))
    recent = engine.recent(limit=10)
    assert len(recent) == 3  # capacity bound
    assert [event.tokens_before for event in recent] == [40, 30, 20]  # newest first


def test_daily_window_resets_on_date_change() -> None:
    engine = AnalyticsEngine()
    engine.record(_event(tokens_before=100, tokens_after=10))
    assert engine.today().requests == 1

    # Simulate the UTC date rolling over.
    engine._today_date = "2000-01-01"
    assert engine.today().requests == 0  # daily window reset
    assert engine.all_time().requests == 1  # all-time preserved


def test_reset_clears_everything() -> None:
    engine = AnalyticsEngine()
    engine.record(_event())
    engine.reset()
    assert engine.all_time().requests == 0
    assert engine.last_event is None


def test_event_from_report_is_metadata_only() -> None:
    result = TransformationResult(
        transformation_name="html",
        detected_content_type=ContentType.HTML,
        transformed_content="markdown",
        original_size_bytes=400,
        transformed_size_bytes=120,
        original_token_count=100,
        transformed_token_count=30,
        execution_time_ms=1.5,
        transformations_applied=("converted_to_markdown",),
        origin="messages[0].content",
        cache_hit=True,
    )
    report = TransformationReport(
        body=b"{}",
        applied=True,
        execution_time_ms=1.5,
        original_size_bytes=400,
        transformed_size_bytes=120,
        results=(result,),
    )
    event = event_from_report(report, provider="openai", endpoint="chat/completions")
    assert event.tokens_before == 100
    assert event.tokens_after == 30
    assert event.tokens_saved == 70
    assert event.cache_hits == 1
    assert event.cache_lookups == 1
    assert event.cache_status == "hit"
    # No attribute could carry content.
    assert "markdown" not in repr(event)


def test_skipped_report_records_a_skip() -> None:
    report = TransformationReport.skipped(b"{}", SkipReason.NOT_MODIFIED, 0.5)
    event = event_from_report(report, provider="anthropic", endpoint="messages")
    assert event.applied is False
    assert event.skip_reason == "content_already_optimal"
    engine = AnalyticsEngine()
    engine.record(event)
    assert engine.all_time().skipped == 1
