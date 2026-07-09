"""Deterministic JSON compaction.

What it does: removes the whitespace a pretty-printer added, and emits non-ASCII
characters directly instead of as ``\\uXXXX`` escapes. A page of indented JSON
pasted into a prompt loses a large fraction of its tokens to indentation alone.

What it does not do: reorder keys, drop fields, coerce types, or infer anything.
Key order is preserved because ``dict`` preserves insertion order, and the output
is generated from the parsed value in that order.

**Duplicate keys.** JSON permits them; ``json.loads`` keeps the last occurrence and
discards the rest. The gateway inherits that behaviour — it does not choose it. The
detector counts duplicates as it parses so the count can be reported rather than
silently swallowed. A payload with duplicate keys is already ambiguous, and every
JSON parser downstream would resolve it the same way.

**Empty containers.** Removing ``[]`` and ``{}`` is off by default. ``{"tools": []}``
tells an API "no tools", which is not what ``{}`` says. Opt in per deployment.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, ClassVar

from gateway.optimizers.base import Transformer
from gateway.optimizers.models import ContentType, TransformOutput
from gateway.optimizers.options import JsonOptions

if TYPE_CHECKING:
    from gateway.optimizers.models import Detection

_COMPACT_SEPARATORS = (",", ":")

STEP_MINIFIED = "minified_json"
STEP_REMOVED_EMPTY = "removed_empty_containers"
STEP_COLLAPSED_DUPLICATE_KEYS = "collapsed_duplicate_keys"


def _is_empty_container(value: Any) -> bool:
    return isinstance(value, (dict, list)) and not value


def _prune_empty(value: Any) -> Any:
    """Recursively drop empty objects and arrays, bottom-up.

    Bottom-up matters: pruning ``{"a": {"b": []}}`` must yield ``{}``, not
    ``{"a": {}}``. Running the prune once from the leaves gives that in one pass,
    and makes the operation idempotent.
    """
    if isinstance(value, dict):
        pruned = {key: _prune_empty(item) for key, item in value.items()}
        return {key: item for key, item in pruned.items() if not _is_empty_container(item)}
    if isinstance(value, list):
        pruned_items = [_prune_empty(item) for item in value]
        return [item for item in pruned_items if not _is_empty_container(item)]
    return value


def _dumps(value: Any) -> str:
    # ensure_ascii=False: "é" is one token, "é" is several.
    return json.dumps(value, ensure_ascii=False, separators=_COMPACT_SEPARATORS)


class JsonTransformer(Transformer):
    """Compacts a JSON document without changing what it means."""

    name: ClassVar[str] = "json"
    priority: ClassVar[int] = 20
    content_types: ClassVar[frozenset[ContentType]] = frozenset({ContentType.JSON})

    def __init__(self, options: JsonOptions | None = None) -> None:
        self._options = options or JsonOptions()

    def transform(self, content: str, detection: Detection) -> TransformOutput:
        # The detector already parsed this. Parsing again would double the cost of
        # the single most expensive step for large payloads.
        parsed = detection.parsed
        if parsed is None:
            try:
                parsed = json.loads(content)
            except (ValueError, RecursionError):
                return TransformOutput(content, ())

        steps: list[str] = []

        if detection.metadata.get("duplicate_keys"):
            steps.append(STEP_COLLAPSED_DUPLICATE_KEYS)

        if self._options.remove_empty_containers:
            pruned = _prune_empty(parsed)
            if pruned != parsed:
                steps.append(STEP_REMOVED_EMPTY)
            parsed = pruned

        try:
            compacted = _dumps(parsed)
        except (TypeError, ValueError, RecursionError):
            return TransformOutput(content, ())

        if compacted == content:
            return TransformOutput(content, ())

        steps.append(STEP_MINIFIED)
        return TransformOutput(compacted, tuple(steps))
