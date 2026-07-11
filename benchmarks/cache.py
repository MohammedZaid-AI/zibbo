"""Reproducible transformation-cache benchmarks.

    python -m benchmarks.cache

Measures the two latencies that matter — a cold transformation (miss: transform, store)
versus a warm one (hit: hash, look up, reuse) — and the CPU the cache saves by turning
the first into the second. Every input is generated in-process from a fixed seed, so the
figures are comparable across machines (absolute times are not, but the *ratio* is the
point).

The cache stores extracted Markdown and its measurements. A warm hit therefore skips
detection, transformation, and token counting alike; for a large PDF, whose base64 is
itself expensive to tokenize, that is most of the cost.
"""

from __future__ import annotations

import base64
import io
import random
import statistics
import time
from dataclasses import dataclass

from gateway.cache import build_transformation_cache
from gateway.config import Settings
from gateway.documents import build_document_service
from gateway.optimizers import build_pipeline, build_provider_policy
from gateway.optimizers.extraction import AdapterRegistry
from gateway.optimizers.models import TransformationRequest
from gateway.providers.anthropic import ANTHROPIC_ENDPOINTS
from gateway.providers.schemas import anthropic_adapters
from gateway.tokenizers import TokenCounterFactory

SEED = 20260711
REPEAT = 20

_WORDS = ["revenue", "margin", "quarter", "growth", "region", "product", "customer", "segment"]


def _settings() -> Settings:
    return Settings(_env_file=None, environment="test", plugins_enabled=False)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    cold_ms: float
    warm_ms: float

    @property
    def speedup(self) -> float:
        return round(self.cold_ms / self.warm_ms, 1) if self.warm_ms else 0.0

    @property
    def cpu_saved_pct(self) -> float:
        return round((self.cold_ms - self.warm_ms) / self.cold_ms * 100, 1) if self.cold_ms else 0.0


def _make_pdf(pages: int) -> bytes:
    from reportlab.pdfgen import canvas

    rng = random.Random(SEED)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for page in range(pages):
        pdf.setFont("Helvetica-Bold", 18)
        pdf.drawString(72, 760, f"Section {page + 1}")
        pdf.setFont("Helvetica", 11)
        y = 730
        for _ in range(30):
            pdf.drawString(72, y, " ".join(rng.choice(_WORDS) for _ in range(12)).capitalize())
            y -= 16
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _make_html() -> bytes:
    rng = random.Random(SEED)
    paras = "".join(f"<p>{' '.join(rng.choice(_WORDS) for _ in range(30))}</p>" for _ in range(40))
    return (
        f"<html><head><style>.x{{color:red}}</style><script>x()</script></head>"
        f"<body><nav>menu</nav><article><h1>Report</h1>{paras}</article>"
        f"<footer>boilerplate</footer></body></html>"
    ).encode()


def _pdf_request(data: bytes) -> bytes:
    import json

    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 8,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": base64.b64encode(data).decode(),
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode()


def _html_request(html: bytes) -> bytes:
    import json

    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 8,
        "messages": [{"role": "user", "content": html.decode()}],
    }
    return json.dumps(payload).encode()


async def _benchmark(name: str, body: bytes) -> Row:
    settings = _settings()
    cache = build_transformation_cache(settings)
    pipeline = build_pipeline(
        settings,
        TokenCounterFactory.from_settings(settings),
        document_service=build_document_service(settings),
        cache=cache,
    )
    policy = build_provider_policy(settings, ANTHROPIC_ENDPOINTS)
    adapters = AdapterRegistry(anthropic_adapters())

    async def run() -> None:
        await pipeline.transform(
            TransformationRequest("POST", "v1/messages", "application/json", body),
            policy=policy,
            adapters=adapters,
        )

    # First call is the cold miss (transform + store).
    cold_start = time.perf_counter()
    await run()
    cold_ms = (time.perf_counter() - cold_start) * 1000

    # Subsequent calls are warm hits.
    warm: list[float] = []
    for _ in range(REPEAT):
        started = time.perf_counter()
        await run()
        warm.append((time.perf_counter() - started) * 1000)

    return Row(name=name, cold_ms=round(cold_ms, 2), warm_ms=round(statistics.median(warm), 3))


async def _amain() -> None:
    counter = TokenCounterFactory().for_model("claude-sonnet-5")
    print(f"token counter: {counter.name} (exact={counter.exact})\n")

    rows = [
        await _benchmark("HTML page", _html_request(_make_html())),
        await _benchmark("10-page PDF", _pdf_request(_make_pdf(10))),
        await _benchmark("100-page PDF", _pdf_request(_make_pdf(100))),
    ]

    header = f"{'workload':<16} {'cold ms':>10} {'warm ms':>10} {'speedup':>9} {'CPU saved':>10}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.name:<16} {row.cold_ms:>10.2f} {row.warm_ms:>10.3f} "
            f"{row.speedup:>8.1f}x {row.cpu_saved_pct:>9.1f}%"
        )


def main() -> None:
    import asyncio

    asyncio.run(_amain())


if __name__ == "__main__":
    main()
