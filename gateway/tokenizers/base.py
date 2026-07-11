"""Token counting.

Every analytic the product sells — tokens saved, money saved — is a difference of
two token counts. The counter is therefore an interface, not a function call into
tiktoken, so that a provider with a different tokenizer (Anthropic, Phase 6) plugs
in without touching the pipeline.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class TokenCounter(ABC):
    """Counts tokens in a string. Must be deterministic and must never raise."""

    name: ClassVar[str]

    @abstractmethod
    def count(self, text: str) -> int:
        """Number of tokens ``text`` would occupy in the model's context."""

    @property
    def exact(self) -> bool:
        """Whether counts match the provider's tokenizer exactly.

        Savings *ratios* stay meaningful even when this is ``False``, because the
        same counter measures both sides of the comparison. Absolute cost figures
        do not, and the analytics layer must say so.
        """
        return False

    @property
    def identity(self) -> str:
        """A stable id for counters that would count the same text differently.

        The transformation cache stores token counts, so it keys on this: a result
        counted under one encoding must not be served to a request using another.
        """
        return self.name
