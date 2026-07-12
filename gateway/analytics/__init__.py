"""In-memory optimization analytics.

Records what the pipeline did — tokens saved, transformers used, cache outcomes,
latency — as pure metadata, and aggregates it for the plugin's ``/zibbo stats`` view.
Nothing here persists across a restart, and nothing here holds request content.

The gateway core stays unaware: the proxy route builds one :class:`OptimizationEvent`
from the report it already has and hands it to the engine. Adding a metric is one field
here, not a change anywhere upstream.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from gateway.analytics.engine import AnalyticsEngine
from gateway.analytics.models import (
    OptimizationEvent,
    TransformerTally,
    WindowStats,
)

if TYPE_CHECKING:
    from gateway.optimizers.models import TransformationReport

__all__ = [
    "AnalyticsEngine",
    "OptimizationEvent",
    "TransformerTally",
    "WindowStats",
    "event_from_report",
]


def event_from_report(
    report: TransformationReport, *, provider: str, endpoint: str
) -> OptimizationEvent:
    """Reduce a ``TransformationReport`` to the metadata the engine aggregates.

    ``cache_lookups`` is the number of segments the pipeline considered — each is a
    potential cache hit — and ``cache_hits`` how many were served from the cache.
    """
    return OptimizationEvent(
        timestamp=time.time(),
        provider=provider,
        endpoint=endpoint,
        applied=report.applied,
        skip_reason=report.skip_reason.value if report.skip_reason else None,
        content_types=report.content_types_detected,
        transformers=report.transformers_used,
        tokens_before=report.original_token_count,
        tokens_after=report.transformed_token_count,
        bytes_before=report.original_size_bytes,
        bytes_after=report.transformed_size_bytes,
        cache_hits=report.cache_hits,
        cache_lookups=len(report.results),
        execution_time_ms=report.execution_time_ms,
    )
