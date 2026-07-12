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

from gateway.cache import CachedTransformation
from gateway.logging import get_logger
from gateway.optimizers.extraction import DocumentSegment, Segment
from gateway.optimizers.models import (
    ContentType,
    SkipReason,
    TransformationReport,
    TransformationResult,
    TransformOutput,
)

if TYPE_CHECKING:
    from gateway.cache import TransformationCache
    from gateway.documents import DocumentFormat, DocumentService
    from gateway.optimizers.detector import ContentDetector
    from gateway.optimizers.extraction import AdapterRegistry
    from gateway.optimizers.models import TransformationRequest
    from gateway.optimizers.options import OptimizerOptions
    from gateway.optimizers.policy import PolicyEngine
    from gateway.optimizers.registry import TransformerRegistry
    from gateway.tokenizers import TokenCounter, TokenCounterFactory

logger = get_logger(__name__)

_DOCUMENT_CONTENT_TYPES: dict[str, ContentType] = {
    "pdf": ContentType.PDF,
    "docx": ContentType.DOCX,
    "csv": ContentType.CSV,
    "xml": ContentType.XML,
    "html": ContentType.HTML,
    "markdown": ContentType.TEXT,
    "text": ContentType.TEXT,
}


def _document_content_type(fmt: DocumentFormat) -> ContentType:
    return _DOCUMENT_CONTENT_TYPES.get(fmt.value, ContentType.BINARY)


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
        document_service: DocumentService | None = None,
        cache: TransformationCache | None = None,
        offload_threshold_bytes: int = 131_072,
    ) -> None:
        self._detector = detector
        self._registry = registry
        self._token_counters = token_counters
        self._options = options
        self._document_service = document_service
        self._cache = cache
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

    def preview(self, content: str, *, model: str | None = None) -> TransformationResult:
        """Run one piece of text through the real transform path, no upstream call.

        This is what ``/internal/benchmark`` uses: it exercises the same detection,
        transformer selection, token counting and *cache* a request would, so the
        numbers it reports are the numbers a request would have seen — but it forwards
        nothing. A throwaway container stands in for the request body; the segment's
        write-back lands there and is discarded.

        Never gated by the enable/disable switch: a benchmark shows what optimization
        *would* do, which is the whole point of running it while it is turned off.
        """
        counter = self._token_counters.for_model(model)
        segment = Segment({"content": content}, "content", content, "benchmark")
        result = self._transform_segment(segment, counter)
        if result is not None:
            return result
        # No transformer applied (or below the size floor): a faithful no-op result.
        tokens = counter.count(content)
        size = len(content.encode("utf-8"))
        return TransformationResult(
            transformation_name="none",
            detected_content_type=self._detector.detect(content).content_type,
            transformed_content=content,
            original_size_bytes=size,
            transformed_size_bytes=size,
            original_token_count=tokens,
            transformed_token_count=tokens,
            execution_time_ms=0.0,
            transformations_applied=(),
            origin="benchmark",
        )

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
            for result in (self._process_segment(segment, counter) for segment in segments)
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

    def _process_segment(
        self, segment: Segment | DocumentSegment, counter: TokenCounter
    ) -> TransformationResult | None:
        if isinstance(segment, DocumentSegment):
            return self._extract_document(segment, counter)
        return self._transform_segment(segment, counter)

    # -- Cache glue --------------------------------------------------------

    def _apply_cached(
        self, cached: CachedTransformation, segment: Segment | DocumentSegment
    ) -> TransformationResult:
        """Rebuild a result from a cache hit and write it back into the payload.

        The stored entry is content-derived; the only thing added back is ``origin``,
        which is where *this* request's segment lived. A hit that actually changed the
        content replays the same ``replace`` a fresh transformation would have done.
        """
        if cached.changed:
            segment.replace(cached.transformed_content)
        return TransformationResult(
            transformation_name=cached.transformation_name,
            detected_content_type=ContentType(cached.content_type),
            transformed_content=cached.transformed_content,
            original_size_bytes=cached.original_size_bytes,
            transformed_size_bytes=cached.transformed_size_bytes,
            original_token_count=cached.original_token_count,
            transformed_token_count=cached.transformed_token_count,
            execution_time_ms=cached.execution_time_ms,
            transformations_applied=cached.steps,
            origin=segment.origin,
            cache_hit=True,
        )

    @staticmethod
    def _to_cache_entry(
        result: TransformationResult, *, transformer_version: str
    ) -> CachedTransformation:
        return CachedTransformation(
            transformation_name=result.transformation_name,
            transformer_version=transformer_version,
            content_type=result.detected_content_type.value,
            transformed_content=result.transformed_content,
            original_size_bytes=result.original_size_bytes,
            transformed_size_bytes=result.transformed_size_bytes,
            original_token_count=result.original_token_count,
            transformed_token_count=result.transformed_token_count,
            steps=result.transformations_applied,
            execution_time_ms=result.execution_time_ms,
        )

    def _extract_document(
        self, segment: DocumentSegment, counter: TokenCounter
    ) -> TransformationResult | None:
        """Decode an embedded document to Markdown, if that is cheaper than the base64.

        The same never-grow guard as text applies, and here it does real work: a
        scanned PDF that yields no text, or a tiny document whose Markdown plus JSON
        overhead exceeds its base64, is left exactly as it arrived.
        """
        if self._document_service is None or not self._document_service.enabled:
            return None

        cache = self._cache if self._cache is not None and self._cache.enabled else None
        key = None
        if cache is not None:
            key = cache.document_key(
                segment.data,
                service_version=self._document_service.version,
                media_type=segment.media_type,
                filename=segment.filename,
                encoding=counter.identity,
            )
            cached = cache.get(key)
            if cached is not None:
                return self._apply_cached(cached, segment)

        result, cacheable = self._compute_document(segment, counter)
        # Only successful extractions are cached. A failed one (encrypted, corrupt,
        # scanned) might succeed on a future request or a fixed extractor, and the spec
        # is explicit: never cache a failed transformation.
        if cache is not None and key is not None and cacheable and result is not None:
            entry = self._to_cache_entry(result, transformer_version=self._document_service.version)
            cache.put(key, entry)
        return result

    def _compute_document(
        self, segment: DocumentSegment, counter: TokenCounter
    ) -> tuple[TransformationResult | None, bool]:
        assert self._document_service is not None  # noqa: S101 — guarded by the caller
        started = time.perf_counter()
        result = self._document_service.extract(
            segment.data, media_type=segment.media_type, filename=segment.filename
        )
        content_type = _document_content_type(result.format)

        if not result.extracted:
            logger.debug(
                "document_not_extracted",
                document_format=result.format.value,
                origin=segment.origin,
                reason=result.detail,
            )
            failed = TransformationResult(
                transformation_name=f"document:{result.format.value}",
                detected_content_type=content_type,
                transformed_content=segment.original_text,
                original_size_bytes=len(segment.original_text.encode("utf-8")),
                transformed_size_bytes=len(segment.original_text.encode("utf-8")),
                original_token_count=0,
                transformed_token_count=0,
                execution_time_ms=_elapsed_ms(started),
                transformations_applied=(),
                origin=segment.origin,
            )
            return failed, False  # extraction failed — never cached

        markdown = result.markdown or ""
        original_tokens = counter.count(segment.original_text)
        transformed_tokens = counter.count(markdown)

        if transformed_tokens >= original_tokens:
            # A pathological case — base64 almost always tokenizes far worse than
            # its extracted text — but the guarantee is unconditional. Extraction still
            # *succeeded* deterministically, so this decision is safe to cache.
            reverted = TransformationResult(
                transformation_name=f"document:{result.format.value}",
                detected_content_type=content_type,
                transformed_content=segment.original_text,
                original_size_bytes=len(segment.original_text.encode("utf-8")),
                transformed_size_bytes=len(segment.original_text.encode("utf-8")),
                original_token_count=original_tokens,
                transformed_token_count=original_tokens,
                execution_time_ms=_elapsed_ms(started),
                transformations_applied=(),
                origin=segment.origin,
            )
            return reverted, True

        segment.replace(markdown)
        extracted = TransformationResult(
            transformation_name=f"document:{result.format.value}",
            detected_content_type=content_type,
            transformed_content=markdown,
            original_size_bytes=len(segment.original_text.encode("utf-8")),
            transformed_size_bytes=len(markdown.encode("utf-8")),
            original_token_count=original_tokens,
            transformed_token_count=transformed_tokens,
            execution_time_ms=_elapsed_ms(started),
            transformations_applied=("extracted_document", f"format_{result.format.value}"),
            origin=segment.origin,
        )
        return extracted, True

    def _transform_segment(
        self, segment: Segment, counter: TokenCounter
    ) -> TransformationResult | None:
        if len(segment.text) < self._options.min_segment_chars:
            return None

        cache = self._cache if self._cache is not None and self._cache.enabled else None
        key = None
        if cache is not None:
            # Key on the whole registry, not the selected transformer: which one runs
            # is itself a deterministic function of the content, so a hit legitimately
            # skips detection, selection, transformation and token counting together.
            key = cache.text_key(
                segment.text,
                transformer_fingerprint=self._registry.fingerprint,
                encoding=counter.identity,
            )
            cached = cache.get(key)
            if cached is not None:
                return self._apply_cached(cached, segment)

        result = self._compute_text(segment, counter)
        if cache is not None and key is not None and result is not None:
            entry = self._to_cache_entry(result, transformer_version=self._registry.fingerprint)
            cache.put(key, entry)
        return result

    def _compute_text(self, segment: Segment, counter: TokenCounter) -> TransformationResult | None:
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
