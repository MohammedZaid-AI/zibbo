"""Deterministic HTML → Markdown.

Removes structural noise and nothing else. There is no summarization, no
rewriting, no model. Every decision below is a rule you can read.

The two subtleties worth knowing:

* **Idempotency.** The transformer's own output is Markdown, which contains no
  tags. Fed its own output, the markup guard fires and it degrades to plain-text
  normalization — the same normalization it already applied as its final step. So
  ``T(T(x)) == T(x)`` holds exactly, not approximately.
* **Conservatism.** Links and image references are kept by default. A URL is
  content; dropping it would save tokens by destroying information, which is the
  one thing this pipeline promises never to do. ``data:`` URIs are the exception:
  they are payload, not reference, and can be megabytes of base64.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, ClassVar, Final

from lxml import html as lxml_html

from gateway.optimizers.base import Transformer
from gateway.optimizers.models import ContentType, TransformOutput
from gateway.optimizers.options import HtmlOptions
from gateway.optimizers.transformers.text import normalize_text

if TYPE_CHECKING:
    from lxml.html import HtmlElement

    from gateway.optimizers.models import Detection

# Cheap gate. If there is no tag-like token, this is not markup, and parsing it
# as HTML would only mangle whitespace.
_MARKUP_RE: Final = re.compile(r"<\s*/?\s*[a-zA-Z]")

# Elements whose content is never prose: code, presentation, or embedded media.
_DROP_TAGS: Final[frozenset[str]] = frozenset(
    {
        "script", "style", "noscript", "template", "svg", "canvas",
        "iframe", "object", "embed", "applet", "param",
        "link", "meta", "base", "head",
        "audio", "video", "source", "track", "map", "area",
    }
)  # fmt: skip

# Site chrome. Present on every page, part of none of them.
_CHROME_TAGS: Final[frozenset[str]] = frozenset({"nav", "aside", "footer", "menu", "dialog"})

# ARIA landmarks that mean "this is chrome".
_NOISE_ROLES: Final[frozenset[str]] = frozenset(
    {"navigation", "banner", "dialog", "alertdialog", "complementary", "search", "contentinfo"}
)

# Word-boundary matching on `-`, `_` and space, so `download` never matches `ad`.
_NOISE_CLASS_RE: Final = re.compile(
    r"(?:^|[\s_-])"
    r"(?:ad|ads|adbox|advert|advertisement|adsense|doubleclick"
    r"|banner|cookie|cookies|consent|gdpr|cmp"
    r"|nav|navbar|navigation|menu|sidebar|side-bar"
    r"|social|share|sharing|newsletter|subscribe"
    r"|popup|modal|overlay|promo|promotion|sponsored"
    r"|breadcrumb|breadcrumbs|pagination|skip-link|screen-reader-text)"
    r"(?:[\s_-]|$)",
    re.IGNORECASE,
)

_HIDDEN_STYLE_RE: Final = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0\s*(?:;|$)|font-size\s*:\s*0)",
    re.IGNORECASE,
)

_HEADINGS: Final[dict[str, int]] = {f"h{level}": level for level in range(1, 7)}
_LISTS: Final[frozenset[str]] = frozenset({"ul", "ol"})
_BLOCK_TAGS: Final[frozenset[str]] = (
    frozenset(_HEADINGS) | _LISTS | {"p", "pre", "blockquote", "table", "hr"}
)
_INLINE_TAGS: Final[frozenset[str]] = frozenset(
    {
        "a", "strong", "b", "em", "i", "u", "s", "strike", "del", "ins",
        "code", "span", "br", "img", "sub", "sup", "small", "mark",
        "abbr", "time", "cite", "q", "kbd", "samp", "var", "label", "font",
    }
)  # fmt: skip

_SAFE_LINK_SCHEMES: Final[tuple[str, ...]] = ("http://", "https://", "mailto:", "ftp://")

# `br` becomes a real newline only after whitespace has been collapsed, so it is
# carried through collapsing as a character that cannot appear in HTML text.
_LINE_BREAK: Final = "\x00"
_WHITESPACE_RE: Final = re.compile(r"\s+")

STEP_REMOVED_SCRIPTS = "removed_scripts"
STEP_REMOVED_STYLES = "removed_styles"
STEP_REMOVED_SVG = "removed_svg"
STEP_REMOVED_EMBEDS = "removed_embedded_media"
STEP_REMOVED_NAVIGATION = "removed_navigation"
STEP_REMOVED_NOISE = "removed_ads_and_banners"
STEP_REMOVED_HIDDEN = "removed_hidden_elements"
STEP_CONVERTED = "converted_to_markdown"
STEP_PRESERVED_TITLE = "preserved_document_title"
STEP_DROPPED_DATA_URIS = "dropped_data_uri_images"


def _clean_inline(text: str) -> str:
    """Collapse every whitespace run to one space, then restore explicit breaks."""
    collapsed = _WHITESPACE_RE.sub(" ", text)
    return collapsed.replace(f" {_LINE_BREAK} ", "\n").replace(_LINE_BREAK, "\n").strip()


def _attr(element: HtmlElement, name: str) -> str:
    value = element.get(name)
    return value if isinstance(value, str) else ""


def _is_hidden(element: HtmlElement) -> bool:
    if element.get("hidden") is not None:
        return True
    if _attr(element, "aria-hidden").lower() == "true":
        return True
    if element.tag == "input" and _attr(element, "type").lower() == "hidden":
        return True
    return bool(_HIDDEN_STYLE_RE.search(_attr(element, "style")))


def _is_noise(element: HtmlElement) -> bool:
    if _attr(element, "role").lower() in _NOISE_ROLES:
        return True
    for attribute in ("class", "id"):
        value = _attr(element, attribute)
        if value and _NOISE_CLASS_RE.search(value):
            return True
    return False


class _MarkdownWriter:
    """Walks a cleaned tree and emits Markdown blocks."""

    def __init__(self, options: HtmlOptions) -> None:
        self._options = options
        self.dropped_data_uris = False

    def render(self, root: HtmlElement) -> str:
        blocks: list[str] = []
        self._walk(root, blocks)
        return "\n\n".join(block for block in blocks if block.strip())

    # -- Block level -------------------------------------------------------

    def _walk(self, element: HtmlElement, blocks: list[str]) -> None:
        """Emit blocks for ``element``'s children, buffering inline runs."""
        inline: list[str] = []
        if element.text:
            inline.append(element.text)

        for child in element:
            tag = child.tag
            if not isinstance(tag, str):  # comment or processing instruction
                continue

            if tag in _INLINE_TAGS:
                inline.append(self._inline(child))
            else:
                self._flush(inline, blocks)
                if tag in _BLOCK_TAGS:
                    self._block(child, blocks)
                else:
                    self._walk(child, blocks)  # unknown/container: transparent

            if child.tail:
                inline.append(child.tail)

        self._flush(inline, blocks)

    @staticmethod
    def _flush(inline: list[str], blocks: list[str]) -> None:
        if not inline:
            return
        text = _clean_inline("".join(inline))
        inline.clear()
        if text:
            blocks.append(text)

    def _block(self, element: HtmlElement, blocks: list[str]) -> None:
        tag = element.tag
        if tag in _HEADINGS:
            text = _clean_inline(self._inline_children(element))
            if text:
                blocks.append(f"{'#' * _HEADINGS[tag]} {text}")
        elif tag == "p":
            text = _clean_inline(self._inline_children(element))
            if text:
                blocks.append(text)
        elif tag in _LISTS:
            rendered = self._list(element, depth=0)
            if rendered:
                blocks.append(rendered)
        elif tag == "pre":
            code = element.text_content().strip("\n")
            if code.strip():
                blocks.append(f"```\n{code}\n```")
        elif tag == "blockquote":
            inner: list[str] = []
            self._walk(element, inner)
            quoted = "\n\n".join(inner)
            if quoted.strip():
                blocks.append("\n".join(f"> {line}".rstrip() for line in quoted.split("\n")))
        elif tag == "table":
            rendered = self._table(element)
            if rendered:
                blocks.append(rendered)
        elif tag == "hr":
            blocks.append("---")

    def _list(self, element: HtmlElement, depth: int) -> str:
        ordered = element.tag == "ol"
        indent = "  " * depth
        lines: list[str] = []
        number = 1

        for item in element:
            if item.tag != "li":
                continue
            inline: list[str] = []
            nested: list[HtmlElement] = []

            if item.text:
                inline.append(item.text)
            for child in item:
                if child.tag in _LISTS:
                    nested.append(child)
                elif isinstance(child.tag, str):
                    inline.append(self._inline(child))
                if child.tail:
                    inline.append(child.tail)

            text = _clean_inline("".join(inline)).replace("\n", " ")
            marker = f"{number}. " if ordered else "- "
            if text:
                lines.append(f"{indent}{marker}{text}")
            for child in nested:
                rendered = self._list(child, depth + 1)
                if rendered:
                    lines.append(rendered)
            number += 1

        return "\n".join(lines)

    def _table(self, element: HtmlElement) -> str:
        rows: list[list[str]] = []
        for row in element.iter("tr"):
            cells = [
                _clean_inline(self._inline_children(cell)).replace("\n", " ").replace("|", r"\|")
                for cell in row
                if cell.tag in ("td", "th")
            ]
            if cells:
                rows.append(cells)

        if not rows:
            return ""

        width = max(len(row) for row in rows)
        padded = [row + [""] * (width - len(row)) for row in rows]
        header, *body = padded

        lines = [f"| {' | '.join(header)} |", f"|{'---|' * width}"]
        lines.extend(f"| {' | '.join(row)} |" for row in body)
        return "\n".join(lines)

    # -- Inline level ------------------------------------------------------

    def _inline_children(self, element: HtmlElement) -> str:
        parts: list[str] = []
        if element.text:
            parts.append(element.text)
        for child in element:
            if isinstance(child.tag, str):
                parts.append(self._inline(child))
            if child.tail:
                parts.append(child.tail)
        return "".join(parts)

    def _inline(self, element: HtmlElement) -> str:
        tag = element.tag
        if tag == "br":
            return _LINE_BREAK
        if tag == "img":
            return self._image(element)

        inner = self._inline_children(element)
        if not inner.strip():
            return inner

        if tag in ("strong", "b"):
            return f"**{inner.strip()}**"
        if tag in ("em", "i"):
            return f"*{inner.strip()}*"
        if tag == "code":
            return f"`{inner.strip()}`"
        if tag == "a":
            return self._link(element, inner)
        return inner

    def _link(self, element: HtmlElement, inner: str) -> str:
        if not self._options.preserve_links:
            return inner
        href = _attr(element, "href").strip()
        if not href.lower().startswith(_SAFE_LINK_SCHEMES):
            return inner  # fragment, javascript:, relative — no value to the model
        return f"[{inner.strip()}]({href})"

    def _image(self, element: HtmlElement) -> str:
        alt = _clean_inline(_attr(element, "alt"))
        src = _attr(element, "src").strip()

        if src.lower().startswith("data:"):
            self.dropped_data_uris = True
            return alt  # keep the description, drop the base64 payload
        if not self._options.preserve_images or not src:
            return alt
        return f"![{alt}]({src})"


class HtmlTransformer(Transformer):
    """Strips chrome and converts the remaining semantic content to Markdown."""

    name: ClassVar[str] = "html"
    priority: ClassVar[int] = 10
    content_types: ClassVar[frozenset[ContentType]] = frozenset({ContentType.HTML})

    def __init__(self, options: HtmlOptions | None = None) -> None:
        self._options = options or HtmlOptions()

    def transform(self, content: str, detection: Detection) -> TransformOutput:
        del detection

        # No markup: this is already Markdown (very likely our own output). Fall
        # through to text normalization, which is what makes T(T(x)) == T(x).
        if not _MARKUP_RE.search(content):
            normalized, text_steps = normalize_text(content, self._options.text)
            if normalized == content:
                return TransformOutput(content, ())
            return TransformOutput(normalized, text_steps)

        # NUL is our line-break sentinel and is never valid in HTML text. Removing
        # it first means content containing one cannot forge a line break.
        source = content.replace(_LINE_BREAK, "")

        try:
            document = lxml_html.document_fromstring(source)
        except (lxml_html.etree.ParserError, ValueError):
            # Unparseable. Forward untouched rather than guess.
            return TransformOutput(content, ())

        steps: list[str] = []
        title = self._extract_title(document)
        self._strip(document, steps)

        body = document.find("body")
        root = body if body is not None else document

        writer = _MarkdownWriter(self._options)
        markdown = writer.render(root)

        if title and not markdown.startswith("# "):
            markdown = f"# {title}\n\n{markdown}" if markdown else f"# {title}"
            steps.append(STEP_PRESERVED_TITLE)
        if writer.dropped_data_uris:
            steps.append(STEP_DROPPED_DATA_URIS)

        # The same normalizer the text transformer uses, so a second pass is a no-op.
        markdown, _ = normalize_text(markdown, self._options.text)

        if markdown == content:
            return TransformOutput(content, ())

        steps.append(STEP_CONVERTED)
        return TransformOutput(markdown, tuple(steps))

    @staticmethod
    def _extract_title(document: HtmlElement) -> str:
        node = document.find(".//title")
        if node is None or not node.text:
            return ""
        return _clean_inline(node.text)

    def _strip(self, document: HtmlElement, steps: list[str]) -> None:
        """Remove noise in one pass over the tree, newest-first so drops are safe."""
        removed: set[str] = set()

        # `iter()` is lazy; materialize before mutating the tree underneath it.
        for element in list(document.iter()):
            tag = element.tag
            if not isinstance(tag, str):
                continue
            if element.getparent() is None:
                continue  # already dropped with an ancestor

            step = self._classify(element, tag)
            if step is None:
                continue
            removed.add(step)
            element.drop_tree()

        steps.extend(sorted(removed))

    def _classify(self, element: HtmlElement, tag: str) -> str | None:
        """Why this element should go, or ``None`` to keep it."""
        if tag in ("script",):
            return STEP_REMOVED_SCRIPTS
        if tag in ("style", "link"):
            return STEP_REMOVED_STYLES
        if tag == "svg":
            return STEP_REMOVED_SVG
        if tag in _DROP_TAGS:
            return STEP_REMOVED_EMBEDS
        if tag in _CHROME_TAGS:
            return STEP_REMOVED_NAVIGATION
        if tag == "header" and not self._inside_article(element):
            return STEP_REMOVED_NAVIGATION
        if _is_hidden(element):
            return STEP_REMOVED_HIDDEN
        if _is_noise(element):
            return STEP_REMOVED_NOISE
        return None

    @staticmethod
    def _inside_article(element: HtmlElement) -> bool:
        """A ``<header>`` inside an article is a byline, not site chrome."""
        return any(ancestor.tag in ("article", "section") for ancestor in element.iterancestors())
