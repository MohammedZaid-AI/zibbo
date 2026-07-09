"""Transformer lookup by content, not by name.

The gateway never imports ``HtmlTransformer``. It asks the registry which
transformer handles this content, and the registry answers. Phase 7 registers PDF,
DOCX and CSV transformers here; nothing upstream of this module changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gateway.errors import ConfigurationError
from gateway.logging import get_logger

if TYPE_CHECKING:
    from gateway.optimizers.base import Transformer
    from gateway.optimizers.models import Detection

logger = get_logger(__name__)


class TransformerRegistry:
    """Holds transformers ordered by priority."""

    def __init__(self) -> None:
        self._transformers: list[Transformer] = []

    def register(self, transformer: Transformer) -> None:
        if any(existing.name == transformer.name for existing in self._transformers):
            raise ConfigurationError(f"transformer {transformer.name!r} is already registered")
        self._transformers.append(transformer)
        # Sort by name as a tiebreaker so selection never depends on registration
        # order — otherwise a reordered lifespan would change gateway behaviour.
        self._transformers.sort(key=lambda item: (item.priority, item.name))
        logger.debug(
            "transformer_registered", transformer=transformer.name, priority=transformer.priority
        )

    def select(self, content: str, detection: Detection) -> Transformer | None:
        """The highest-priority transformer that accepts this content, if any."""
        for transformer in self._transformers:
            if transformer.can_handle(content, detection):
                return transformer
        return None

    @property
    def transformers(self) -> tuple[Transformer, ...]:
        return tuple(self._transformers)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(transformer.name for transformer in self._transformers)
