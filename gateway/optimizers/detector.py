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


# Markdown code regions. Anything inside them is a *quotation* of code, never code
# the user is asking the model to treat as content.
_FENCED_CODE_RE: Final = re.compile(r"(?P<fence>```|~~~).*?(?P=fence)", re.DOTALL)
_INLINE_CODE_RE: Final = re.compile(r"`[^`\n]*`")

# `<!-- ... -->` and stray whitespace may legitimately precede a document's first tag.
_LEADING_NOISE_RE: Final = re.compile(r"\A(?:\s|<!--.*?-->)*", re.DOTALL)
_OPENS_WITH_TAG_RE: Final = re.compile(r"\A<\s*[a-zA-Z][a-zA-Z0-9]{0,14}\b")

# Markup so dense it cannot be prose, whatever it starts with.
_DENSE_MARKUP_RATIO: Final = 0.5


def strip_code_regions(content: str) -> str:
    """Blank out fenced and inline code so quoted markup is not counted as markup."""
    without_fences = _FENCED_CODE_RE.sub(" ", content)
    return _INLINE_CODE_RE.sub(" ", without_fences)


class HtmlSniffer:
    """Is this markup *used as content*, or markup being *talked about*?

    Counting tags cannot answer that. Real documents and prose-about-HTML overlap
    on every count-based measure — a generated encyclopedia article has a markup
    density of 0.155, and the sentence "To make a paragraph use ``<p>text</p>``,
    to bold use ``<b>text</b>``" has 0.119. A threshold between them does not exist.

    What does separate them is where the markup *starts*. A pasted HTML document
    begins with a tag. Prose about HTML begins with words. So:

    1. Code regions are removed first. Markup inside a fenced block or backticks is
       a quotation — documentation, a question, a bug report — never content.
    2. A doctype or ``<html>``/``<body>``/``<head>`` settles it: that is a document.
    3. Otherwise the content must *open* with a tag and close at least two, or be
       more than half markup by character count.

    Everything else is prose, and prose is normalized rather than rewritten. The
    failure this avoids is silent: treat "what does ``<p>hello</p>`` do?" as HTML
    and the user's question becomes "what does hello do?".

    A side effect the pipeline depends on: Markdown has no closing tags, so the
    HTML transformer's own output is never re-detected as HTML. That is what makes
    ``pipeline(pipeline(x)) == pipeline(x)`` exact.
    """

    name: ClassVar[str] = "html-structure"

    _DOCTYPE_RE: ClassVar[re.Pattern[str]] = re.compile(r"<!doctype\s+html", re.IGNORECASE)
    _TAG_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]{0,14})\b[^>]*>"
    )

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
        # Markup quoted inside code regions is discussed, not used.
        prose = strip_code_regions(content)

        if self._DOCTYPE_RE.search(prose):
            return Detection(ContentType.HTML, 0.99, self.name)

        distinct: set[str] = set()
        closing = 0
        markup_chars = 0
        for match in self._TAG_RE.finditer(prose):
            tag = match.group(2).lower()
            if tag not in self._KNOWN_TAGS:
                continue
            distinct.add(tag)
            markup_chars += len(match.group())
            if match.group(1):
                closing += 1

        if not distinct:
            return None
        if distinct & self._DOCUMENT_TAGS:
            return Detection(ContentType.HTML, 0.99, self.name)

        if closing < 2:
            return None

        density = markup_chars / max(len(prose.strip()), 1)
        opens_with_tag = bool(_OPENS_WITH_TAG_RE.match(_LEADING_NOISE_RE.sub("", prose)))

        if opens_with_tag or density > _DENSE_MARKUP_RATIO:
            confidence = min(0.95, 0.8 + 0.02 * len(distinct))
            return Detection(
                ContentType.HTML,
                confidence,
                self.name,
                metadata={"markup_density": round(density, 3), "opens_with_tag": opens_with_tag},
            )
        return None


class PlainTextSniffer:
    """Always matches, at a confidence low enough to lose to anything else."""

    name: ClassVar[str] = "fallback"

    def sniff(self, content: str) -> Detection | None:
        return Detection(ContentType.TEXT, 0.1, self.name)


# A line that reads as source code, a log record, or a stack-trace frame. Prompts that
# are *mostly* these are pastes to be preserved verbatim, not prose to de-duplicate.
_CODE_OR_LOG_RE: Final = re.compile(
    r"""
    ^\s*(?:
        at\s+\S+                               # java/js stack frame
      | File\s+"[^"]+",\s+line\s+\d+           # python traceback frame
      | Traceback\s+\(most\s+recent\s+call     # python traceback header
      | (?:ERROR|WARN|WARNING|INFO|DEBUG|TRACE|FATAL)\b   # log level
      | \d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}       # iso timestamp prefix
      | (?:import|from|def|class|function|const|let|var|public|private|return|if|for|while)\b
      | [#@}/*]                                # comment / decorator / brace / path lines
    )
    """,
    re.VERBOSE,
)


class PromptSniffer:
    """Classify long, duplicate-heavy prose/Markdown as an optimizable PROMPT.

    Only fires when prompt optimization is enabled *and* the content clears two gates:
    it is long enough that de-duplication is worth the work, and a meaningful fraction
    of its lines are exact duplicates. Structural formats (JSON, HTML, XML) are already
    claimed at higher confidence by their own sniffers, so this never overrides them;
    and content that reads as code, logs or a stack trace is refused outright, because
    those are pastes to forward untouched, never prompts to reshape.

    The confidence (0.8) sits above the detector's trust threshold but below the
    structural sniffers, so PROMPT wins over plain text and loses to real markup.
    """

    name: ClassVar[str] = "prompt-structure"

    def __init__(self, *, min_chars: int, min_duplicate_ratio: float) -> None:
        self._min_chars = min_chars
        self._min_duplicate_ratio = min_duplicate_ratio

    def sniff(self, content: str) -> Detection | None:
        if len(content) < self._min_chars:
            return None

        non_blank = [line for line in content.splitlines() if line.strip()]
        if len(non_blank) < 2:
            return None

        code_or_log = sum(1 for line in non_blank if _CODE_OR_LOG_RE.match(line))
        if code_or_log / len(non_blank) > 0.6:
            return None

        seen: set[str] = set()
        duplicates = 0
        for line in non_blank:
            key = line.strip()
            if key in seen:
                duplicates += 1
            else:
                seen.add(key)
        ratio = duplicates / len(non_blank)
        if ratio < self._min_duplicate_ratio:
            return None

        return Detection(
            ContentType.PROMPT,
            0.8,
            self.name,
            metadata={"duplicate_ratio": round(ratio, 3), "lines": len(non_blank)},
        )


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
        self._sniffers = list(sniffers) if sniffers is not None else list(default_sniffers())
        self._threshold = confidence_threshold

    @property
    def sniffers(self) -> tuple[Sniffer, ...]:
        return tuple(self._sniffers)

    def add_sniffer(self, sniffer: Sniffer) -> None:
        """Register a sniffer contributed by a plugin (or the runtime prompt toggle).

        Order does not matter — ``detect`` takes the highest confidence, not the
        first match — so a new sniffer is simply appended. Copy-on-write, so adding one
        at runtime never tears a ``detect`` iterating in a worker thread.
        """
        if any(existing.name == sniffer.name for existing in self._sniffers):
            raise ValueError(f"sniffer {sniffer.name!r} is already registered")
        self._sniffers = [*self._sniffers, sniffer]

    def has_sniffer(self, name: str) -> bool:
        return any(existing.name == name for existing in self._sniffers)

    def remove_sniffer(self, name: str) -> None:
        """Remove a sniffer. Idempotent, so a rollback can call it blindly."""
        self._sniffers = [item for item in self._sniffers if item.name != name]

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
