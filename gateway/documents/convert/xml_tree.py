"""XML -> readable Markdown.

Removes the formatting noise — indentation, attribute clutter, namespace prefixes —
while preserving the hierarchy, because the hierarchy *is* the meaning of an XML
document. Elements become nested headings and bullet lists; leaf text becomes prose;
attributes are kept but demoted, since they usually carry real data (an ``id``, a
``date``) that must not be dropped.

Uses lxml's recovering parser: real-world XML exports are frequently not
well-formed, and a document we cannot parse is returned untouched, never rejected.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from lxml import etree

if TYPE_CHECKING:
    from lxml.etree import _Element

_WHITESPACE_RE: Final = re.compile(r"\s+")
_MAX_HEADING_DEPTH: Final = 6

# Guardrails: a maliciously deep or huge tree should not exhaust the stack or memory.
_MAX_DEPTH: Final = 100
_MAX_NODES: Final = 50_000


def _local_name(tag: object) -> str:
    """Strip a namespace: ``{http://...}title`` -> ``title``."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _clean(text: str | None) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip() if text else ""


def _attributes(element: _Element) -> str:
    parts = [f"{_local_name(k)}={v}" for k, v in element.attrib.items() if _clean(str(v))]
    return f" ({', '.join(parts)})" if parts else ""


class _Renderer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.nodes = 0

    def render(self, element: _Element, depth: int) -> None:
        self.nodes += 1
        if depth > _MAX_DEPTH or self.nodes > _MAX_NODES:
            return

        tag = _local_name(element.tag)
        if not tag:  # comment or processing instruction
            return

        children = [child for child in element if isinstance(child.tag, str)]
        text = _clean(element.text)
        attrs = _attributes(element)

        if not children:
            # A leaf: "tag (attrs): text", or just the heading if it is empty.
            label = f"**{tag}**{attrs}"
            self.lines.append(f"{label}: {text}" if text else label)
            return

        if depth < _MAX_HEADING_DEPTH:
            self.lines.append(f"{'#' * (depth + 1)} {tag}{attrs}")
        else:
            self.lines.append(f"**{tag}**{attrs}")
        if text:
            self.lines.append(text)

        for child in children:
            self.render(child, depth + 1)
            tail = _clean(child.tail)
            if tail:
                self.lines.append(tail)


def xml_to_markdown(text: str) -> str | None:
    """Convert an XML document to Markdown. ``None`` if it cannot be parsed at all."""
    stripped = text.strip()
    if not stripped:
        return None

    parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=False)
    try:
        root = etree.fromstring(stripped.encode("utf-8"), parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        return None
    if root is None:
        return None

    renderer = _Renderer()
    renderer.render(root, 0)
    body = "\n\n".join(line for line in renderer.lines if line.strip())
    return body or None
