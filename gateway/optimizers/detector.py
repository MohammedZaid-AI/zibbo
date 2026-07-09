"""Content detection.

The declared ``Content-Type`` is a hint, not evidence. A user pastes a scraped
web page into a chat message and the transport says ``application/json`` — the
message *content* is HTML and nothing in the headers says so. So detection is
primarily body inspection, with the declared type used only to break ties.

Each signal is a :class:`Sniffer`. Adding PDF, DOCX, CSV or image detection in
Phase 7 means appending a sniffer, not editing the detector.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol

from gateway.optimizers.models import ContentType, Detection

if TYPE_CHECKING:
    from collections.abc import Sequence

# A sniff at or above this is trusted over the declared Content-Type.
CONFIDENCE_THRESHOLD: Final = 0.7

_MIME_MAP: Final[dict[str, ContentType]] = {
    "application/json": ContentType.JSON,
    "text/json": ContentType.JSON,
    "text/html": ContentType.HTML,
    "application/xhtml+xml": ContentType.HTML,
    "application/xml": ContentType.XML,
    "text/xml": ContentType.XML,
    "text/csv": ContentType.CSV,
    "text/plain": ContentType.TEXT,
    "text/markdown": ContentType.TEXT,
    "application/pdf": ContentType.PDF,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ContentType.DOCX,
    "application/octet-stream": ContentType.BINARY,
}


def content_type_from_mime(mime: str | None) -> ContentType | None:
    """Map a ``Content-Type`` header onto a :class:`ContentType`, ignoring params."""
    if not mime:
        return None
    essence = mime.split(";", 1)[0].strip().lower()
    if not essence:
        return None
    if essence.startswith("image/"):
        return ContentType.IMAGE
    if essence.startswith("multipart/"):
        return ContentType.BINARY
    return _MIME_MAP.get(essence)


class Sniffer(Protocol):
    """Inspects content and, if it recognizes it, reports what it found."""

    name: ClassVar[str]

    def sniff(self, content: str) -> Detection | None: ...


class MagicBytesSniffer:
    """File signatures.

    Present and wired up now so Phase 7 (PDF, DOCX, CSV) only adds table entries.
    Matches on the textual prefix, since the pipeline operates on decoded segments.
    """

    name: ClassVar[str] = "magic-bytes"

    _SIGNATURES: ClassVar[tuple[tuple[str, ContentType], ...]] = (
        ("%PDF-", ContentType.PDF),
        ("PK\x03\x04", ContentType.DOCX),  # also .zip/.xlsx; Phase 7 disambiguates
        ("\x89PNG\r\n", ContentType.IMAGE),
        ("\xff\xd8\xff", ContentType.IMAGE),
        ("GIF87a", ContentType.IMAGE),
        ("GIF89a", ContentType.IMAGE),
    )

    def sniff(self, content: str) -> Detection | None:
        prefix = content[:8]
        for signature, content_type in self._SIGNATURES:
            if prefix.startswith(signature):
                return Detection(content_type, 1.0, self.name)
        return None


class JsonSniffer:
    """Parses the content. If it parses, it is JSON — no heuristic needed.

    The parse result rides along on the ``Detection`` so the JSON transformer does
    not repeat it. Duplicate keys are counted here, where the raw pairs are still
    visible; ``json.loads`` discards that fact.
    """

    name: ClassVar[str] = "json-parse"

    def sniff(self, content: str) -> Detection | None:
        stripped = content.strip()
        # Cheap gate: avoid handing megabytes of prose to the JSON parser.
        if len(stripped) < 2 or stripped[0] not in "{[" or stripped[-1] not in "}]":
            return None

        duplicates = 0

        def object_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            nonlocal duplicates
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    duplicates += 1
                result[key] = value
            return result

        try:
            parsed = json.loads(stripped, object_pairs_hook=object_pairs_hook)
        except (ValueError, RecursionError):
            return None

        return Detection(
            ContentType.JSON,
            0.99,
            self.name,
            parsed=parsed,
            metadata={"duplicate_keys": duplicates},
        )


class XmlSniffer:
    name: ClassVar[str] = "xml-declaration"

    _RE: ClassVar[re.Pattern[str]] = re.compile(r"^\s*<\?xml\s", re.IGNORECASE)

    def sniff(self, content: str) -> Detection | None:
        if self._RE.match(content):
            return Detection(ContentType.XML, 0.95, self.name)
        return None


class HtmlSniffer:
    """Structural detection: a doctype, a document element, or closed tag pairs.

    The signal is **closing tags**, not tag variety. Markdown — which is what the
    HTML transformer emits — has none, so its output can never be re-detected as
    HTML, which is exactly what makes the pipeline idempotent.

    Requiring *two* closing tags also protects meaning. A user asking "what does
    ``<p>hello</p>`` do?" is writing prose about markup, not sending markup. Treat
    that as HTML and the transformer would rewrite their question to "what does
    hello do?" — a semantic corruption, and the one thing this pipeline promises
    never to commit. One closing tag is prose; a document has many.
    """

    name: ClassVar[str] = "html-structure"

    _DOCTYPE_RE: ClassVar[re.Pattern[str]] = re.compile(r"<!doctype\s+html", re.IGNORECASE)
    _TAG_RE: ClassVar[re.Pattern[str]] = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]{0,14})\b")

    # Their presence means "this is a document", regardless of how it is closed.
    _DOCUMENT_TAGS: ClassVar[frozenset[str]] = frozenset({"html", "body", "head"})

    _KNOWN_TAGS: ClassVar[frozenset[str]] = frozenset(
        {
            "html", "head", "body", "div", "span", "p", "a", "img", "br", "hr",
            "h1", "h2", "h3", "h4", "h5", "h6",
            "ul", "ol", "li", "dl", "dt", "dd",
            "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
            "script", "style", "link", "meta", "title", "noscript", "svg",
            "nav", "header", "footer", "aside", "main", "article", "section",
            "form", "input", "button", "select", "option", "textarea", "label",
            "strong", "b", "em", "i", "u", "code", "pre", "blockquote",
            "figure", "figcaption", "picture", "source", "iframe", "video", "audio",
        }
    )  # fmt: skip

    def sniff(self, content: str) -> Detection | None:
        if self._DOCTYPE_RE.search(content):
            return Detection(ContentType.HTML, 0.99, self.name)

        distinct: set[str] = set()
        closing = 0
        for slash, raw_tag in self._TAG_RE.findall(content):
            tag = raw_tag.lower()
            if tag not in self._KNOWN_TAGS:
                continue
            distinct.add(tag)
            if slash:
                closing += 1

        if not distinct:
            return None
        if distinct & self._DOCUMENT_TAGS:
            return Detection(ContentType.HTML, 0.99, self.name)
        if closing >= 2 or (closing >= 1 and len(distinct) >= 3):
            return Detection(ContentType.HTML, min(0.95, 0.8 + 0.02 * len(distinct)), self.name)
        return None


class PlainTextSniffer:
    """Always matches, at a confidence low enough to lose to anything else."""

    name: ClassVar[str] = "fallback"

    def sniff(self, content: str) -> Detection | None:
        return Detection(ContentType.TEXT, 0.1, self.name)


def default_sniffers() -> tuple[Sniffer, ...]:
    return (
        MagicBytesSniffer(),
        XmlSniffer(),
        JsonSniffer(),
        HtmlSniffer(),
        PlainTextSniffer(),
    )


class ContentDetector:
    """Combines every signal into one verdict."""

    def __init__(
        self,
        sniffers: Sequence[Sniffer] | None = None,
        *,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
    ) -> None:
        self._sniffers = tuple(sniffers) if sniffers is not None else default_sniffers()
        self._threshold = confidence_threshold

    def detect(self, content: str, declared_mime: str | None = None) -> Detection:
        """Identify ``content``. Body evidence beats the declared type."""
        best: Detection | None = None
        for sniffer in self._sniffers:
            detection = sniffer.sniff(content)
            if detection is not None and (best is None or detection.confidence > best.confidence):
                best = detection

        if best is not None and best.confidence >= self._threshold:
            return best

        declared = content_type_from_mime(declared_mime)
        if declared is not None:
            return Detection(declared, 0.5, "content-type-header")

        return best or Detection(ContentType.UNKNOWN, 0.0, "none")
