"""Benchmark corpora, generated deterministically.

Vendored fixtures would bloat the repository; downloading real pages would make the
numbers depend on when you ran them and whether you had a network. Instead the
documents are *generated* from a fixed seed, so byte counts and token counts are
identical on every machine, forever, offline.

The structure is what matters, and it is drawn from what these pages actually look
like: a Wikipedia article is mostly prose, references and infobox chrome; a
documentation page is headings, prose and code; a news article is one-third
content and two-thirds advertising, navigation and consent banners.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

SEED = 20260709

_WORDS = [
    "gateway",
    "proxy",
    "latency",
    "token",
    "context",
    "window",
    "inference",
    "throughput",
    "cache",
    "deterministic",
    "pipeline",
    "transformer",
    "semantic",
    "payload",
    "markdown",
    "document",
    "response",
    "request",
    "provider",
    "optimize",
    "compress",
    "structure",
    "content",
    "upstream",
    "analytics",
    "measurement",
    "heuristic",
    "encoding",
    "boundary",
    "threshold",
    "interface",
]


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    description: str
    content: str


def _rng() -> random.Random:
    return random.Random(SEED)


def _sentence(rng: random.Random, words: int = 12) -> str:
    body = " ".join(rng.choice(_WORDS) for _ in range(words))
    return body.capitalize() + "."


def _paragraph(rng: random.Random, sentences: int = 5) -> str:
    return " ".join(_sentence(rng) for _ in range(sentences))


# -- HTML chrome that real pages carry -------------------------------------


def _boilerplate_head() -> str:
    return (
        "<head><title>Reference Article</title>"
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        '<link rel="stylesheet" href="/static/main.css">'
        '<link rel="stylesheet" href="/static/print.css">'
        "<style>" + ".content{margin:0 auto;max-width:60rem}" * 40 + "</style>"
        '<script src="/static/analytics.js"></script>'
        "<script>"
        + "window.dataLayer=window.dataLayer||[];dataLayer.push({e:1});"
        * 30
        + "</script>"
        "</head>"
    )


def _navigation(rng: random.Random, links: int = 40) -> str:
    items = "".join(
        f'<li><a href="/wiki/{rng.choice(_WORDS)}">{rng.choice(_WORDS).title()}</a></li>'
        for _ in range(links)
    )
    return f'<nav class="site-navigation"><ul>{items}</ul></nav>'


def _cookie_banner() -> str:
    return (
        '<div id="cookie-consent-banner" class="consent-overlay">'
        "<p>We and our 843 partners store and access information on a device.</p>"
        '<button class="accept-all">Accept All</button>'
        '<button class="manage">Manage Preferences</button></div>'
    )


def _advertisements(count: int = 6) -> str:
    return "".join(
        f'<div class="ad-slot" data-slot="{index}">'
        f'<iframe src="https://ads.example/{index}"></iframe>'
        f"<span>Sponsored content you might like</span></div>"
        for index in range(count)
    )


def _hidden_tracking() -> str:
    return (
        '<div style="display:none">tracking pixel identifier 9f2c1e4b</div>'
        '<span aria-hidden="true">&#9662;</span>'
        '<input type="hidden" name="csrf" value="a8f3d9e1c7b2">'
        '<div class="screen-reader-text">Skip to main content</div>'
    )


def _footer() -> str:
    return (
        '<footer class="site-footer"><div class="social-share">'
        "<a href='#'>Share on X</a><a href='#'>Share on Facebook</a></div>"
        "<p>© 2026 Reference Foundation. Text is available under CC BY-SA.</p></footer>"
    )


# -- Datasets --------------------------------------------------------------


def wikipedia_article() -> Dataset:
    rng = _rng()
    sections = []
    for _ in range(12):
        heading = f"<h2>{_sentence(rng, 3).rstrip('.')}</h2>"
        paragraphs = "".join(f"<p>{_paragraph(rng)}</p>" for _ in range(4))
        sections.append(heading + paragraphs)

    references = "".join(
        f'<li id="cite-{index}">{_sentence(rng, 8)} '
        f'<a href="https://doi.example/{index}">doi:10.1000/{index}</a></li>'
        for index in range(45)
    )

    infobox = (
        '<table class="infobox"><tr><th>Field</th><th>Value</th></tr>'
        + "".join(
            f"<tr><td>{rng.choice(_WORDS).title()}</td><td>{rng.choice(_WORDS)}</td></tr>"
            for _ in range(10)
        )
        + "</table>"
    )

    content = (
        "<!DOCTYPE html><html>"
        + _boilerplate_head()
        + "<body>"
        + _navigation(rng, links=60)
        + _cookie_banner()
        + '<aside class="sidebar">'
        + _paragraph(rng)
        + "</aside>"
        + "<main><article><h1>Reference Article</h1>"
        + infobox
        + "".join(sections)
        + f"<h2>References</h2><ol>{references}</ol>"
        + "</article></main>"
        + _hidden_tracking()
        + _footer()
        + "</body></html>"
    )
    return Dataset("wikipedia_article", "Encyclopedia article with infobox and references", content)


def documentation_page() -> Dataset:
    rng = _rng()
    sections = []
    for index in range(10):
        sections.append(
            f"<h2>Section {index}</h2>"
            f"<p>{_paragraph(rng, 3)}</p>"
            f"<pre><code>from gateway import Client\n"
            f"client = Client(base_url='http://localhost:8000/v1')\n"
            f"client.call({index})</code></pre>"
            f"<ul>{''.join(f'<li>{_sentence(rng, 6)}</li>' for _ in range(4))}</ul>"
        )

    content = (
        "<!DOCTYPE html><html>"
        + _boilerplate_head()
        + "<body>"
        + _navigation(rng, links=35)
        + '<div class="sidebar-navigation">'
        + "".join(f"<a href='#s{i}'>Section {i}</a>" for i in range(10))
        + "</div>"
        + "<main><h1>API Documentation</h1>"
        + "".join(sections)
        + "<table><tr><th>Option</th><th>Type</th><th>Default</th></tr>"
        + "".join(
            f"<tr><td>--{rng.choice(_WORDS)}</td><td>string</td><td>none</td></tr>"
            for _ in range(12)
        )
        + "</table></main>"
        + _footer()
        + "</body></html>"
    )
    return Dataset("documentation_page", "Docs page with code blocks and an options table", content)


def news_article() -> Dataset:
    rng = _rng()
    paragraphs = "".join(f"<p>{_paragraph(rng)}</p>" for _ in range(8))
    content = (
        "<!DOCTYPE html><html>"
        + _boilerplate_head()
        + "<body>"
        + _navigation(rng, links=25)
        + _cookie_banner()
        + _advertisements(8)
        + '<div class="newsletter-signup"><p>Subscribe for our daily briefing</p></div>'
        + "<main><article><header><h1>Headline Of The Day</h1>"
        + "<p>By A Correspondent</p></header>"
        + paragraphs
        + "</article></main>"
        + _advertisements(4)
        + '<div class="related-posts">'
        + "".join(f"<a href='#'>{_sentence(rng, 5)}</a>" for _ in range(10))
        + "</div>"
        + _hidden_tracking()
        + _footer()
        + "</body></html>"
    )
    return Dataset("news_article", "News page: mostly ads, banners and navigation", content)


def json_api_response() -> Dataset:
    rng = _rng()
    payload: dict[str, Any] = {
        "meta": {"page": 1, "per_page": 50, "total": 1284, "request_id": "req_9f2c1e"},
        "data": [
            {
                "id": index,
                "name": f"{rng.choice(_WORDS)}-{index}",
                "description": _sentence(rng, 10),
                "tags": [rng.choice(_WORDS) for _ in range(3)],
                "metrics": {
                    "latency_ms": round(rng.uniform(1, 400), 3),
                    "tokens": rng.randint(10, 5000),
                    "ratio": round(rng.random(), 6),
                },
                "created_at": "2026-01-01T00:00:00Z",
                "nested": {"a": {"b": {"c": rng.choice(_WORDS)}}},
            }
            for index in range(60)
        ],
    }
    # Pretty-printed, as an API returns it and as a user pastes it.
    return Dataset(
        "json_api_response", "Pretty-printed JSON API response", json.dumps(payload, indent=4)
    )


def plain_text_notes() -> Dataset:
    rng = _rng()
    blocks: list[str] = []
    for index in range(30):
        blocks.append(_paragraph(rng, 4) + "   ")
        if index % 5 == 0:
            blocks.append(blocks[-1])  # a duplicated paragraph, as copy-paste produces
    return Dataset(
        "plain_text_notes",
        "Meeting notes with trailing spaces, blank runs and duplicate paragraphs",
        "\r\n\r\n\r\n".join(blocks),
    )


def all_datasets() -> tuple[Dataset, ...]:
    return (
        wikipedia_article(),
        documentation_page(),
        news_article(),
        json_api_response(),
        plain_text_notes(),
    )
