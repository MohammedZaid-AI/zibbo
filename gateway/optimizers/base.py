"""The Transformer interface.

A transformer takes text and returns text plus the names of the steps it applied.
It does not time itself, count its own tokens, or measure its own bytes — the
pipeline does that for every transformer identically. Adding a metric later is one
change in ``models.py``, not one change per transformer.

Two invariants every transformer must uphold, enforced by property-based tests:

* **Deterministic.** ``T(x)`` is the same on every call, in every process.
* **Idempotent.** ``T(T(x)) == T(x)`` whenever ``T`` can handle its own output.

And one it must never violate: a transformer removes structural noise. It never
summarizes, reorders meaning, or invents text.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from gateway.optimizers.models import ContentType, Detection, TransformOutput


class Transformer(ABC):
    """Rewrites one piece of content of one kind."""

    name: ClassVar[str]

    version: ClassVar[str] = "1"
    """Bumped when the transformer's output for the same input changes. It is part of
    the transformation cache key (via the registry fingerprint), so incrementing it
    invalidates every entry this transformer produced without touching the store."""

    priority: ClassVar[int]
    """Lower runs first. The registry picks the first transformer that can handle
    the content, so a specific transformer must outrank a general one: HTML (10)
    before JSON (20) before plain text (100)."""

    content_types: ClassVar[frozenset[ContentType]]
    """The kinds this transformer claims. Consulted by the default ``can_handle``."""

    def can_handle(self, content: str, detection: Detection) -> bool:
        """Whether this transformer should run.

        Override to add content-level conditions beyond the detected type.
        """
        del content
        return detection.content_type in self.content_types

    @abstractmethod
    def transform(self, content: str, detection: Detection) -> TransformOutput:
        """Return the rewritten content and the steps applied.

        An empty ``steps`` tuple means "nothing to do"; the pipeline treats that as
        unchanged and forwards the original bytes. Must never raise on valid input,
        and must never raise on invalid input either — malformed content is
        forwarded untouched, not rejected.
        """
