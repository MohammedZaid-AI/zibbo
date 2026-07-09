"""Token counting per provider family.

``TokenCounterFactory.for_model`` is the only entry point. It returns an exact
tiktoken counter when the encoding is available and a deterministic heuristic
counter when it is not, so token accounting degrades in accuracy rather than
failing the request.
"""

from gateway.tokenizers.base import TokenCounter
from gateway.tokenizers.heuristic import HeuristicTokenCounter
from gateway.tokenizers.registry import TokenCounterFactory
from gateway.tokenizers.tiktoken_counter import TiktokenCounter, TiktokenUnavailableError

__all__ = [
    "HeuristicTokenCounter",
    "TiktokenCounter",
    "TiktokenUnavailableError",
    "TokenCounter",
    "TokenCounterFactory",
]
