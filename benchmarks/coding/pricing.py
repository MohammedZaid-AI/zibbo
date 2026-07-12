"""Per-provider input-token list prices, for turning tokens saved into an estimate.

These are published list prices in USD per **million input tokens**, used only to express
a saving in dollars. They are estimates, not invoices: prices change, and a real bill
depends on the model, output tokens, and any discounts. The benchmark reports say so.

A provider entry also names the tiktoken encoding to count with, so "tokens" in a report
means "tokens as this provider's tokenizer would count them" rather than a single global
approximation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Provider:
    key: str
    label: str
    model: str
    """A representative model id; also selects the tiktoken encoding."""

    usd_per_million_input_tokens: float


# Representative list prices as of the benchmark's authoring. Update alongside the
# datasets, never silently. See docs/BENCHMARKS.md.
PROVIDERS: dict[str, Provider] = {
    "openai": Provider("openai", "OpenAI GPT-4o", "gpt-4o", 2.50),
    "anthropic": Provider("anthropic", "Claude Sonnet", "claude-sonnet-4", 3.00),
    "openai-mini": Provider("openai-mini", "OpenAI GPT-4o mini", "gpt-4o-mini", 0.15),
}

DEFAULT_PROVIDER = "openai"


def estimate_cost(tokens: int, provider: Provider) -> float:
    """USD for ``tokens`` input tokens at this provider's list price."""
    return round(tokens / 1_000_000 * provider.usd_per_million_input_tokens, 6)
