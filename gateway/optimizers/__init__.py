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

from typing import TYPE_CHECKING

from gateway.optimizers.detector import ContentDetector
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
from gateway.optimizers.policy import PolicyEngine
from gateway.optimizers.registry import TransformerRegistry
from gateway.optimizers.transformers import HtmlTransformer, JsonTransformer, TextTransformer

if TYPE_CHECKING:
    from gateway.config import Settings
    from gateway.tokenizers import TokenCounterFactory

__all__ = [
    "AdapterRegistry",
    "ContentDetector",
    "ContentType",
    "Detection",
    "HtmlTransformer",
    "JsonTransformer",
    "OptimizerOptions",
    "PolicyEngine",
    "SkipReason",
    "TextTransformer",
    "TransformOutput",
    "TransformationPipeline",
    "TransformationReport",
    "TransformationRequest",
    "TransformationResult",
    "TransformerRegistry",
    "build_pipeline",
    "build_transformer_registry",
]


def build_transformer_registry(options: OptimizerOptions) -> TransformerRegistry:
    """The one place transformers are named. Phase 7 appends here."""
    registry = TransformerRegistry()
    registry.register(HtmlTransformer(options.html))
    registry.register(JsonTransformer(options.json))
    registry.register(TextTransformer(options.text))
    return registry


def build_pipeline(
    settings: Settings, token_counters: TokenCounterFactory
) -> TransformationPipeline:
    """Assemble the pipeline from configuration."""
    options = OptimizerOptions.from_settings(settings)
    return TransformationPipeline(
        detector=ContentDetector(),
        registry=build_transformer_registry(options),
        policy=PolicyEngine.from_settings(settings),
        adapters=AdapterRegistry(),
        token_counters=token_counters,
        options=options,
        offload_threshold_bytes=settings.optimization_offload_threshold_bytes,
    )
