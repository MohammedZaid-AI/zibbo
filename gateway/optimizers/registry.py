"""Transformer lookup by content, not by name.

The gateway never imports ``HtmlTransformer``. It asks the registry which
transformer handles this content, and the registry answers. Phase 7 registers PDF,
DOCX and CSV transformers here; nothing upstream of this module changes.
"""

from __future__ import annotations

import hashlib
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

    def unregister(self, name: str) -> None:
        """Remove a transformer. Idempotent, so a rollback can call it blindly."""
        self._transformers = [item for item in self._transformers if item.name != name]

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

    @property
    def fingerprint(self) -> str:
        """A stable digest of every registered transformer's name and version.

        The transformation cache keys on this rather than on the single transformer
        that ran, because *which* transformer runs is itself a deterministic function
        of the content — so the whole registry is the unit that must stay fixed for a
        cached output to remain valid. Registering, removing, or version-bumping any
        transformer (a plugin included) changes the digest and retires the old cache.
        """
        material = ";".join(
            f"{transformer.name}@{transformer.version}"
            for transformer in sorted(self._transformers, key=lambda item: item.name)
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
