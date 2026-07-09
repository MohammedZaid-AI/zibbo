"""tiktoken-backed exact token counting.

tiktoken resolves an encoding by **downloading** its BPE file on first use and
caching it under ``TIKTOKEN_CACHE_DIR``. That network call is the reason this
module exists as a separate, failure-tolerant unit:

* Loading is lazy and happens once per encoding, guarded by a lock so that a
  hundred concurrent requests trigger one download, not a hundred.
* A load failure is not an error. It is logged once and the caller falls back to
  the heuristic counter, because a gateway must not 500 because a CDN is down.

In production the cache is baked into the Docker image at build time, so the
runtime never reaches for the network at all.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, ClassVar

from gateway.logging import get_logger
from gateway.tokenizers.base import TokenCounter

if TYPE_CHECKING:
    import tiktoken

logger = get_logger(__name__)

_lock = threading.Lock()
_encodings: dict[str, tiktoken.Encoding] = {}
_failed: set[str] = set()


class TiktokenUnavailableError(RuntimeError):
    """The encoding could not be loaded. Callers should degrade, not propagate."""


def load_encoding(name: str) -> tiktoken.Encoding:
    """Return a cached encoding, loading it at most once per process."""
    cached = _encodings.get(name)
    if cached is not None:
        return cached
    if name in _failed:
        raise TiktokenUnavailableError(name)

    with _lock:
        # Re-check: another thread may have loaded it while we waited.
        cached = _encodings.get(name)
        if cached is not None:
            return cached
        if name in _failed:
            raise TiktokenUnavailableError(name)

        try:
            import tiktoken

            encoding = tiktoken.get_encoding(name)
        except Exception as exc:
            _failed.add(name)
            logger.warning(
                "tiktoken_encoding_unavailable",
                encoding=name,
                cause=type(exc).__name__,
                detail="falling back to the heuristic token counter",
            )
            raise TiktokenUnavailableError(name) from exc

        _encodings[name] = encoding
        logger.info("tiktoken_encoding_loaded", encoding=name)
        return encoding


def encoding_name_for_model(model: str | None, default: str) -> str:
    """Map a model id onto an encoding name, without loading anything.

    Note ``tiktoken.model.encoding_name_for_model``, not ``tiktoken.encoding_for_model``:
    the latter returns an ``Encoding``, which means downloading it. This must stay a
    pure lookup so that choosing a counter never touches the network.
    """
    if not model:
        return default
    try:
        from tiktoken.model import encoding_name_for_model as lookup

        return lookup(model)
    except Exception:  # noqa: BLE001
        # Unknown or future model, or tiktoken missing entirely. The default
        # encoding is the right guess for any recent OpenAI model.
        return default


class TiktokenCounter(TokenCounter):
    """Exact counts for OpenAI models."""

    name: ClassVar[str] = "tiktoken"

    def __init__(self, encoding_name: str) -> None:
        self._encoding_name = encoding_name
        self._encoding = load_encoding(encoding_name)

    @property
    def encoding_name(self) -> str:
        return self._encoding_name

    @property
    def exact(self) -> bool:
        return True

    def count(self, text: str) -> int:
        if not text:
            return 0
        # disallowed_special=() so that a user pasting "<|endoftext|>" counts it as
        # ordinary text instead of raising.
        return len(self._encoding.encode(text, disallowed_special=()))
