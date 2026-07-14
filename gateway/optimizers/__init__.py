"""Deterministic content optimization.

The gateway calls exactly one thing here: ``TransformationPipeline.transform``.
It never imports a transformer. Transformers are found through the registry, by
what the content *is*, not by what the caller claims it is.

Optimizers strip structural noise only. They never summarize, rewrite, reorder, or
invent. Every transformation is deterministic and idempotent.

Phase 3 ships HTML, JSON, and plain text. Phase 7 adds PDF, DOCX and CSV by adding
modules under ``transformers/`` and one line in ``build_transformer_registry``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from gateway.optimizers.detector import ContentDetector, PromptSniffer
from gateway.optimizers.extraction import AdapterRegistry
from gateway.optimizers.models import (
    ContentType,
    Detection,
    SkipReason,
    TransformationReport,
    TransformationRequest,
    TransformationResult,
    TransformOutput,
)
from gateway.optimizers.options import OptimizerOptions
from gateway.optimizers.pipeline import TransformationPipeline
from gateway.optimizers.policy import EndpointPolicy, PolicyEngine
from gateway.optimizers.registry import TransformerRegistry
from gateway.optimizers.transformers import (
    HtmlTransformer,
    JsonTransformer,
    PromptTransformer,
    TextTransformer,
)

if TYPE_CHECKING:
    from gateway.cache import TransformationCache
    from gateway.config import Settings
    from gateway.documents import DocumentService
    from gateway.runtime import RuntimeControl
    from gateway.tokenizers import TokenCounterFactory

__all__ = [
    "AdapterRegistry",
    "ContentDetector",
    "ContentType",
    "Detection",
    "EndpointPolicy",
    "HtmlTransformer",
    "JsonTransformer",
    "OptimizerOptions",
    "PolicyEngine",
    "PromptSniffer",
    "PromptTransformer",
    "SkipReason",
    "TextTransformer",
    "TransformOutput",
    "TransformationPipeline",
    "TransformationReport",
    "TransformationRequest",
    "TransformationResult",
    "TransformerRegistry",
    "apply_prompt_optimization",
    "build_pipeline",
    "build_provider_policy",
    "build_transformer_registry",
]

_PROMPT_SNIFFER_NAME = "prompt-structure"


def build_transformer_registry(options: OptimizerOptions) -> TransformerRegistry:
    """The one place transformers are named.

    The prompt transformer is registered only when prompt optimization is enabled, so a
    deployment that never turns it on carries a registry — and a cache fingerprint —
    identical to before the feature existed. The runtime ``zibbo enable/disable prompt``
    goes through :func:`apply_prompt_optimization` to add or remove it live.
    """
    registry = TransformerRegistry()
    registry.register(HtmlTransformer(options.html))
    registry.register(JsonTransformer(options.json))
    if options.prompt.enabled:
        registry.register(PromptTransformer(options.prompt))
    registry.register(TextTransformer(options.text))
    return registry


def apply_prompt_optimization(
    registry: TransformerRegistry,
    detector: ContentDetector,
    options: OptimizerOptions,
    *,
    enabled: bool,
) -> None:
    """Add or remove the prompt transformer and its detector, idempotently.

    Both the transformer and the sniffer move together so detection and transformation
    never disagree. Because the transformer's presence changes the registry fingerprint,
    a cache entry produced while the feature was off is never mistaken for one produced
    while it was on — the two states occupy different cache namespaces. Safe to call from
    the request-handling event loop; the registry and detector are copy-on-write.
    """
    if enabled:
        if PromptTransformer.name not in registry.names:
            # ``enabled`` is the intent for *this* call; ``options.prompt.enabled`` is the
            # boot-time value, frozen when the app started. On a boot-off → runtime-enable
            # they disagree, and constructing the transformer from the stale boot value
            # would register a transformer whose own ``enabled`` guard is False — selected
            # but a no-op. Registration already means "active", so force the flag to match.
            registry.register(PromptTransformer(replace(options.prompt, enabled=True)))
        if not detector.has_sniffer(_PROMPT_SNIFFER_NAME):
            detector.add_sniffer(
                PromptSniffer(
                    min_chars=options.prompt.min_chars,
                    min_duplicate_ratio=options.prompt.min_duplicate_ratio,
                )
            )
    else:
        registry.unregister(PromptTransformer.name)
        detector.remove_sniffer(_PROMPT_SNIFFER_NAME)


def build_pipeline(
    settings: Settings,
    token_counters: TokenCounterFactory,
    *,
    registry: TransformerRegistry | None = None,
    detector: ContentDetector | None = None,
    document_service: DocumentService | None = None,
    cache: TransformationCache | None = None,
) -> TransformationPipeline:
    """Assemble the provider-agnostic pipeline from configuration.

    ``registry`` and ``detector`` are injectable so that the plugin manager can
    attach to them *before* the pipeline is built. This module knows nothing about
    plugins, and must not: the dependency runs the other way.

    ``document_service`` extracts embedded PDF/DOCX/etc. documents; when ``None``,
    document blocks are left untouched. Per-provider policy and adapters are *not*
    built here — they are passed to :meth:`TransformationPipeline.transform` per
    request by the provider that owns the route.
    """
    options = OptimizerOptions.from_settings(settings)
    detector = detector if detector is not None else ContentDetector()
    registry = registry if registry is not None else build_transformer_registry(options)
    # Keep the prompt sniffer and transformer in lockstep whenever a pipeline is built
    # standalone (benchmarks, tests). In the app, lifespan has already done this against
    # the live runtime flag; the call is idempotent, so repeating it here is harmless.
    apply_prompt_optimization(registry, detector, options, enabled=options.prompt.enabled)
    return TransformationPipeline(
        detector=detector,
        registry=registry,
        token_counters=token_counters,
        options=options,
        document_service=document_service,
        cache=cache,
        offload_threshold_bytes=settings.optimization_offload_threshold_bytes,
    )


def build_provider_policy(
    settings: Settings,
    endpoint_policy: EndpointPolicy,
    control: RuntimeControl | None = None,
) -> PolicyEngine:
    """The optimization policy for one provider: global rules plus its endpoints.

    ``control`` makes the kill switch live: when supplied, the enable/disable state is
    read from it per request instead of frozen from settings at startup.
    """
    return PolicyEngine.from_settings(settings, endpoint_policy, control)
