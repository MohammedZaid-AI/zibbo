"""An offline, dependency-free token counter.

Used when tiktoken's encoding files are unavailable — they are downloaded on first
use, and a gateway must not fail a request because a CDN is unreachable.

The regex mirrors the shape of the GPT pre-tokenizer: contractions, a leading
space bound to the following word, punctuation runs, and whitespace runs. Long
words are then split further, since BPE breaks them into multiple tokens. This
lands within roughly 10% of tiktoken on English prose — good enough for a
*ratio*, not good enough for a bill.
"""

from __future__ import annotations

import re
from typing import ClassVar

from gateway.tokenizers.base import TokenCounter

_PRETOKEN_RE = re.compile(
    r"'(?:[sdmt]|ll|ve|re)| ?[^\W\d_]+| ?\d+| ?[^\s\w]+|\s+",
    re.UNICODE,
)

# Average characters per BPE token inside a single long word.
_CHARS_PER_SUBWORD = 6


class HeuristicTokenCounter(TokenCounter):
    """Approximates GPT tokenization without loading an encoding."""

    name: ClassVar[str] = "heuristic"

    def count(self, text: str) -> int:
        if not text:
            return 0

        total = 0
        for match in _PRETOKEN_RE.finditer(text):
            piece = match.group()
            if piece.isspace():
                # A run of whitespace is usually one token; long runs are more.
                total += 1 + (len(piece) - 1) // 16
                continue
            stripped = piece.strip()
            total += 1 + max(0, (len(stripped) - 1) // _CHARS_PER_SUBWORD)
        return total
