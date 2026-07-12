"""Wire schemas for the /internal/* API the Zibbo plugin consumes.

Every field is deployment metadata or an aggregate count. Nothing here can carry a
request's content — the same guarantee the analytics engine holds, surfaced at the API
boundary so the plugin can display freely.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict

INTERNAL_API_VERSION: Final = "1"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ProviderInfo(_Frozen):
    name: str
    prefix: str


class StatusResponse(_Frozen):
    """Everything the plugin's activation banner needs in one call."""

    name: str
    version: str
    environment: str
    uptime_seconds: float
    optimization_enabled: bool
    pipeline_active: bool
    cache_enabled: bool
    cache_backend: str
    documents_enabled: bool
    transformers: list[str]
    providers: list[ProviderInfo]


class TopTransformer(_Frozen):
    name: str
    count: int
    tokens_saved: int


class WindowStatsModel(_Frozen):
    requests: int
    optimized: int
    skipped: int
    tokens_before: int
    tokens_after: int
    tokens_saved: int
    token_reduction_pct: float
    bytes_saved: int
    cache_hit_rate: float
    transformations: int
    avg_latency_ms: float
    top_transformer: TopTransformer | None
    estimated_cost_usd: float | None


class StatsResponse(_Frozen):
    date: str
    today: WindowStatsModel
    all_time: WindowStatsModel


class VersionResponse(_Frozen):
    gateway_version: str
    internal_api_version: str
    app_name: str


class BenchmarkRequest(_Frozen):
    """Benchmark a supplied sample, or — when ``content`` is omitted — replay the last
    request's recorded metadata."""

    content: str | None = None
    model: str | None = None


class BenchmarkResponse(_Frozen):
    source: str
    content_type: str | None
    original_tokens: int
    optimized_tokens: int
    reduction_pct: float
    transformers: list[str]
    cache_used: bool
    processing_time_ms: float
    note: str | None = None


class ToggleResponse(_Frozen):
    optimization_enabled: bool


class DoctorCheck(_Frozen):
    name: str
    status: str  # ok | warn | fail
    detail: str
    fix: str | None = None


class DoctorResponse(_Frozen):
    healthy: bool
    checks: list[DoctorCheck]


class LogEvent(_Frozen):
    timestamp: float
    provider: str
    endpoint: str
    applied: bool
    skip_reason: str | None
    content_types: list[str]
    transformers: list[str]
    steps: list[str]
    tokens_before: int
    tokens_after: int
    tokens_saved: int
    cache_status: str | None
    execution_time_ms: float


class LogsResponse(_Frozen):
    count: int
    events: list[LogEvent]
