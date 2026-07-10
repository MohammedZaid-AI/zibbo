"""The transformation pipeline.

The gateway calls ``pipeline.transform(request)`` and gets back a body to forward
plus a report. It does not know that HTML exists.

    policy -> parse -> adapter -> for each segment: detect -> select -> transform
           -> reassemble -> report

Three properties this file is responsible for:

**Transparency by default.** If nothing changed, the *original bytes* are
forwarded — not a re-serialization of them. A request whose content was already
optimal crosses the gateway byte-for-byte, exactly as it did in Phase 2.

**No blocking.** Parsing a multi-megabyte HTML document takes tens of milliseconds
of pure CPU. Above a threshold the work moves to a worker thread, because holding
the event loop stalls every other in-flight request, including streams.

**No user content in logs.** Sizes, token counts, timings, transformer names.
Never a byte of what the user sent.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

import anyio.to_thread

from gateway.logging import get_logger
from gateway.optimizers.models import (
    SkipReason,
    TransformationReport,
    TransformationResult,
    TransformOutput,
)

if TYPE_CHECKING:
    from gateway.optimizers.detector import ContentDetector
    from gateway.optimizers.extraction import AdapterRegistry, Segment
    from gateway.optimizers.models import TransformationRequest
    from gateway.optimizers.options import OptimizerOptions
    from gateway.optimizers.policy import PolicyEngine
    from gateway.optimizers.registry import TransformerRegistry
    from gateway.tokenizers import TokenCounter, TokenCounterFactory

logger = get_logger(__name__)


class TransformationPipeline:
    """Detects, transforms, measures, reports.

    The pipeline is **provider-agnostic**. Detection, transformation, token counting
    and measurement are the same whatever the request is bound for. What differs by
    provider — which endpoints are eligible (``policy``) and where the prose lives in
    the body (``adapters``) — is passed to :meth:`transform` per call, supplied by the
    provider that owns the route. So one pipeline (one plugin registry, one detector)
    serves every provider, and no provider knowledge leaks into this module.
    """

    def __init__(
        self,
        *,
        detector: ContentDetector,
        registry: TransformerRegistry,
        token_counters: TokenCounterFactory,
        options: OptimizerOptions,
        offload_threshold_bytes: int = 131_072,
    ) -> None:
        self._detector = detector
        self._registry = registry
        self._token_counters = token_counters
        self._options = options
        self._offload_threshold_bytes = offload_threshold_bytes

    async def transform(
        self,
        request: TransformationRequest,
        *,
        policy: PolicyEngine,
        adapters: AdapterRegistry,
    ) -> TransformationReport:
        """Optimize ``request`` if the provider's policy allows. Never raises."""
        decision = policy.decide(request)
        if not decision.optimize:
            assert decision.reason is not None  # noqa: S101 — a deny always carries a reason
            report = TransformationReport.skipped(request.body, decision.reason)
            logger.debug("optimization_skipped", reason=decision.reason.value, rule=decision.rule)
            return report

        if len(request.body) >= self._offload_threshold_bytes:
            report = await anyio.to_thread.run_sync(self._run, request, adapters)
        else:
            report = self._run(request, adapters)

        self._log(report)
        return report

    # -- Synchronous core, safe to run in a worker thread ------------------

    def _run(
        self, request: TransformationRequest, adapters: AdapterRegistry
    ) -> TransformationReport:
        started = time.perf_counter()

        try:
            payload = json.loads(request.body)
        except (ValueError, RecursionError):
            return TransformationReport.skipped(request.body, SkipReason.MALFORMED_PAYLOAD)
        if not isinstance(payload, dict):
            return TransformationReport.skipped(request.body, SkipReason.MALFORMED_PAYLOAD)

        adapter = adapters.for_path(request.path)
        if adapter is None:
            return TransformationReport.skipped(request.body, SkipReason.NO_ADAPTER)

        segments = list(adapter.extract(payload))
        if not segments:
            return TransformationReport.skipped(request.body, SkipReason.NO_SEGMENTS)

        counter = self._token_counters.for_model(_model_of(payload))
        results = [
            result
            for result in (self._transform_segment(segment, counter) for segment in segments)
            if result is not None
        ]

        if not any(result.changed for result in results):
            return TransformationReport.skipped(
                request.body, SkipReason.NOT_MODIFIED, _elapsed_ms(started)
            )

        # Segments already wrote themselves back into `payload` in place.
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        return TransformationReport(
            body=body,
            applied=True,
            execution_time_ms=_elapsed_ms(started),
            original_size_bytes=len(request.body),
            transformed_size_bytes=len(body),
            results=tuple(results),
        )

    def _transform_segment(
        self, segment: Segment, counter: TokenCounter
    ) -> TransformationResult | None:
        if len(segment.text) < self._options.min_segment_chars:
            return None

        started = time.perf_counter()
        detection = self._detector.detect(segment.text)
        transformer = self._registry.select(segment.text, detection)
        if transformer is None:
            return None

        try:
            output = transformer.transform(segment.text, detection)
        except Exception:
            logger.exception(
                "transformer_failed",
                transformer=transformer.name,
                content_type=detection.content_type.value,
            )
            return None

        original_tokens = counter.count(segment.text)
        transformed_tokens = counter.count(output.content) if output.steps else original_tokens

        # Optimization must never cost more than it saves. A transformer can grow its
        # input — Markdown table pipes outweigh CSV commas on a narrow table, and a
        # short HTML fragment can gain more syntax than it sheds. When that happens
        # the result is discarded and the original forwarded, so the worst a
        # transformer can do to a bill is nothing. This belongs here rather than in
        # each transformer: it is a property of the product, not of any one format.
        if output.steps and transformed_tokens > original_tokens:
            logger.debug(
                "transformation_reverted",
                transformer=transformer.name,
                content_type=detection.content_type.value,
                tokens_before=original_tokens,
                tokens_after=transformed_tokens,
            )
            output = TransformOutput(segment.text, ())
            transformed_tokens = original_tokens

        if output.steps:
            segment.replace(output.content)

        return TransformationResult(
            transformation_name=transformer.name,
            detected_content_type=detection.content_type,
            transformed_content=output.content,
            original_size_bytes=len(segment.text.encode("utf-8")),
            transformed_size_bytes=len(output.content.encode("utf-8")),
            original_token_count=original_tokens,
            transformed_token_count=transformed_tokens,
            execution_time_ms=_elapsed_ms(started),
            transformations_applied=output.steps,
            origin=segment.origin,
        )

    # -- Observability -----------------------------------------------------

    @staticmethod
    def _log(report: TransformationReport) -> None:
        """Metadata only. The request id rides along on the log contextvars."""
        if not report.applied:
            logger.debug(
                "optimization_skipped",
                reason=report.skip_reason.value if report.skip_reason else None,
            )
            return

        logger.info(
            "optimization_applied",
            transformers=report.transformers_used,
            content_types=report.content_types_detected,
            segments=len(report.results),
            tokens_before=report.original_token_count,
            tokens_after=report.transformed_token_count,
            tokens_saved=report.tokens_saved,
            token_reduction_pct=report.token_reduction_pct,
            bytes_before=report.original_size_bytes,
            bytes_after=report.transformed_size_bytes,
            bytes_saved=report.bytes_saved,
            execution_time_ms=report.execution_time_ms,
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _model_of(payload: dict[str, Any]) -> str | None:
    model = payload.get("model")
    return model if isinstance(model, str) else None
