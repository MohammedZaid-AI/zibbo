"""In-memory analytics aggregation.

Records one :class:`OptimizationEvent` per request and answers the plugin's questions:
how many requests today, the cache hit rate, tokens saved, the top transformer, average
latency. It is the "analytics engine" the plugin reads through ``/internal/stats``.

**In-memory only, by deliberate design.** Counters live in this process and reset when
it restarts; nothing is written to a database or disk. Persisted analytics is a separate,
approval-gated piece of work — this engine is what makes the plugin useful without it.

Thread-safe: recorded from the request path (sometimes a worker thread), read from the
event loop when an endpoint is hit. One lock guards all of it; the critical sections are
integer updates.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from gateway.analytics.models import TransformerTally, WindowStats

if TYPE_CHECKING:
    from collections.abc import Iterable

    from gateway.analytics.models import OptimizationEvent

_RECENT_CAPACITY = 100


@dataclass(slots=True)
class _Accumulator:
    """Mutable running totals for one window. Snapshotted into an immutable WindowStats."""

    requests: int = 0
    optimized: int = 0
    skipped: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    transformations: int = 0
    latency_ms_sum: float = 0.0
    latency_samples: int = 0
    transformer_count: dict[str, int] = field(default_factory=dict)
    transformer_saved: dict[str, int] = field(default_factory=dict)

    def add(self, event: OptimizationEvent) -> None:
        self.requests += 1
        self.latency_ms_sum += event.execution_time_ms
        self.latency_samples += 1
        if event.applied:
            self.optimized += 1
            self.tokens_before += event.tokens_before
            self.tokens_after += event.tokens_after
            self.bytes_before += event.bytes_before
            self.bytes_after += event.bytes_after
            self.transformations += len(event.transformers)
            for name in event.transformers:
                self.transformer_count[name] = self.transformer_count.get(name, 0) + 1
        else:
            self.skipped += 1
        self.cache_hits += event.cache_hits
        self.cache_misses += max(0, event.cache_lookups - event.cache_hits)
        # Per-transformer token savings: attribute the request's saving evenly across
        # the transformers that ran, so "top transformer" reflects contribution.
        if event.applied and event.transformers and event.tokens_saved:
            share = event.tokens_saved // len(event.transformers)
            for name in event.transformers:
                self.transformer_saved[name] = self.transformer_saved.get(name, 0) + share

    def snapshot(self) -> WindowStats:
        per_transformer = {
            name: TransformerTally(
                name=name,
                count=count,
                tokens_saved=self.transformer_saved.get(name, 0),
            )
            for name, count in self.transformer_count.items()
        }
        return WindowStats(
            requests=self.requests,
            optimized=self.optimized,
            skipped=self.skipped,
            tokens_before=self.tokens_before,
            tokens_after=self.tokens_after,
            bytes_before=self.bytes_before,
            bytes_after=self.bytes_after,
            cache_hits=self.cache_hits,
            cache_misses=self.cache_misses,
            transformations=self.transformations,
            latency_ms_sum=self.latency_ms_sum,
            latency_samples=self.latency_samples,
            per_transformer=per_transformer,
        )


class AnalyticsEngine:
    """Aggregates optimization events over the process lifetime and the current day."""

    def __init__(self, *, recent_capacity: int = _RECENT_CAPACITY) -> None:
        self._lock = threading.Lock()
        self._all_time = _Accumulator()
        self._today = _Accumulator()
        self._today_date = self._current_date()
        self._recent: deque[OptimizationEvent] = deque(maxlen=recent_capacity)
        self._last_event: OptimizationEvent | None = None
        self._started_at = time.time()

    @staticmethod
    def _current_date() -> str:
        return datetime.now(UTC).strftime("%Y-%m-%d")

    def _roll_day_if_needed(self) -> None:
        """Reset the daily window when the UTC date changes. Caller holds the lock."""
        today = self._current_date()
        if today != self._today_date:
            self._today = _Accumulator()
            self._today_date = today

    def record(self, event: OptimizationEvent) -> None:
        """Fold one event into both windows. Cheap; never raises on valid input."""
        with self._lock:
            self._roll_day_if_needed()
            self._all_time.add(event)
            self._today.add(event)
            self._recent.append(event)
            self._last_event = event

    def today(self) -> WindowStats:
        with self._lock:
            self._roll_day_if_needed()
            return self._today.snapshot()

    def all_time(self) -> WindowStats:
        with self._lock:
            return self._all_time.snapshot()

    def recent(self, limit: int = 20) -> list[OptimizationEvent]:
        """The most recent events, newest first. Metadata only."""
        with self._lock:
            events = list(self._recent)
        return list(reversed(events))[:limit]

    @property
    def last_event(self) -> OptimizationEvent | None:
        with self._lock:
            return self._last_event

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def today_date(self) -> str:
        with self._lock:
            return self._today_date

    def reset(self) -> None:
        """Drop all counters. For tests, and a possible future ``/zibbo reset``."""
        with self._lock:
            self._all_time = _Accumulator()
            self._today = _Accumulator()
            self._today_date = self._current_date()
            self._recent.clear()
            self._last_event = None

    def ingest(self, events: Iterable[OptimizationEvent]) -> None:
        """Bulk-record, for tests and replay. Each event goes through :meth:`record`."""
        for event in events:
            self.record(event)
