"""Resolves a model id to a token counter, with graceful degradation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from gateway.config import TokenizerBackend
from gateway.tokenizers.heuristic import HeuristicTokenCounter
from gateway.tokenizers.tiktoken_counter import (
    TiktokenCounter,
    TiktokenUnavailableError,
    encoding_name_for_model,
)

if TYPE_CHECKING:
    from gateway.config import Settings
    from gateway.tokenizers.base import TokenCounter


class TokenCounterFactory:
    """Hands out counters, one per encoding, cached for the process lifetime."""

    def __init__(
        self,
        *,
        backend: TokenizerBackend = TokenizerBackend.AUTO,
        default_encoding: str = "o200k_base",
    ) -> None:
        self._backend = backend
        self._default_encoding = default_encoding
        self._heuristic = HeuristicTokenCounter()
        self._cache: dict[str, TokenCounter] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> TokenCounterFactory:
        return cls(
            backend=settings.tokenizer,
            default_encoding=settings.tokenizer_default_encoding,
        )

    def for_model(self, model: str | None) -> TokenCounter:
        """A counter appropriate for ``model``. Never raises."""
        if self._backend is TokenizerBackend.HEURISTIC:
            return self._heuristic

        encoding = encoding_name_for_model(model, self._default_encoding)
        cached = self._cache.get(encoding)
        if cached is not None:
            return cached

        try:
            counter: TokenCounter = TiktokenCounter(encoding)
        except TiktokenUnavailableError:
            if self._backend is TokenizerBackend.TIKTOKEN:
                # Explicitly requested: keep trying, so a transient outage recovers
                # rather than pinning the process to approximate counts forever.
                return self._heuristic
            counter = self._heuristic

        self._cache[encoding] = counter
        return counter

    def prewarm(self) -> bool:
        """Load the default encoding ahead of the first request.

        Returns whether exact counting is available. Called at startup so the
        network fetch, if any, happens before traffic arrives rather than inside
        a user's request.
        """
        return self.for_model(None).exact
