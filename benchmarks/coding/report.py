"""Turn a suite run into the published artifacts.

Writes four files into ``benchmarks/results/``:

* ``results.json`` — the canonical, **deterministic** record: per-case token/byte
  reductions, transformers, and per-provider cost. No wall-clock timing and no
  timestamp, so it regenerates byte-for-byte on the same tokenizer.
* ``summary.csv`` — one row per case, for spreadsheets.
* ``report.md`` — the human-readable report (includes machine-dependent timing).
* ``website-stats.json`` — the landing-page numbers, straight from the results.

It also rewrites the benchmark block in ``README.md`` between marker comments, so the
README's numbers are never hand-edited.

Timing is the one figure that is not reproducible — it depends on the machine. It lives
only in ``report.md`` and ``website-stats.json``, never in the deterministic record.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import TYPE_CHECKING

from benchmarks.coding.pricing import PROVIDERS, estimate_cost
from benchmarks.coding.runner import run_suite, tokenizer_is_exact

if TYPE_CHECKING:
    from benchmarks.coding.models import SuiteResult

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"
README = Path(__file__).resolve().parents[2] / "README.md"
README_START = "<!-- BENCHMARKS:START -->"
README_END = "<!-- BENCHMARKS:END -->"

SCHEMA = 1


# -- Deterministic record ----------------------------------------------------


def results_document(suite: SuiteResult, *, tokenizer_exact: bool) -> dict:
    """The canonical results.json content. Deterministic: no timing, no timestamp."""
    cost_by_provider = [
        {
            "key": provider.key,
            "label": provider.label,
            "model": provider.model,
            "usd_per_million_input_tokens": provider.usd_per_million_input_tokens,
            "cost_before_usd": estimate_cost(suite.total_original_tokens, provider),
            "cost_after_usd": estimate_cost(suite.total_optimized_tokens, provider),
            "cost_saved_usd": round(
                estimate_cost(suite.total_original_tokens, provider)
                - estimate_cost(suite.total_optimized_tokens, provider),
                6,
            ),
        }
        for provider in PROVIDERS.values()
    ]
    return {
        "schema": SCHEMA,
        "tokenizer_exact": tokenizer_exact,
        "measured_with": {"provider": suite.provider_key, "model": suite.model},
        "totals": {
            "cases": len(suite.cases),
            "cases_helped": suite.cases_helped,
            "original_tokens": suite.total_original_tokens,
            "optimized_tokens": suite.total_optimized_tokens,
            "tokens_saved": suite.total_tokens_saved,
            "original_bytes": suite.total_original_bytes,
            "optimized_bytes": suite.total_optimized_bytes,
            "overall_token_reduction_pct": suite.overall_token_reduction_pct,
            "avg_token_reduction_pct": suite.avg_token_reduction_pct,
            "cache_hit_rate": suite.cache_hit_rate,
        },
        "cost_by_provider": cost_by_provider,
        "file_types": [
            {
                "content_type": stat.content_type,
                "cases": stat.cases,
                "avg_token_reduction_pct": stat.avg_token_reduction_pct,
            }
            for stat in suite.file_type_stats()
        ],
        "top_transformers": [
            {"name": tc.name, "count": tc.count} for tc in suite.top_transformers()
        ],
        "cases": [
            {
                "id": case.case_id,
                "project": case.project,
                "scenario": case.scenario,
                "content_type": case.content_type,
                "original_bytes": case.original_bytes,
                "optimized_bytes": case.optimized_bytes,
                "bytes_removed": case.bytes_removed,
                "original_tokens": case.original_tokens,
                "optimized_tokens": case.optimized_tokens,
                "tokens_saved": case.tokens_saved,
                "token_reduction_pct": case.token_reduction_pct,
                "cache_hit": case.cache_hit,
                "transformers": list(case.transformers),
            }
            for case in sorted(suite.cases, key=lambda c: c.case_id)
        ],
    }


def summary_csv(suite: SuiteResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "id",
            "project",
            "scenario",
            "content_type",
            "original_tokens",
            "optimized_tokens",
            "token_reduction_pct",
            "bytes_removed",
            "cache_hit",
            "transformers",
        ]
    )
    for case in sorted(suite.cases, key=lambda c: c.case_id):
        writer.writerow(
            [
                case.case_id,
                case.project,
                case.scenario,
                case.content_type,
                case.original_tokens,
                case.optimized_tokens,
                case.token_reduction_pct,
                case.bytes_removed,
                "hit" if case.cache_hit else "miss",
                " ".join(case.transformers),
            ]
        )
    return buffer.getvalue()


# -- Website assets ----------------------------------------------------------


def website_stats(suite: SuiteResult) -> dict:
    """Landing-page numbers, straight from the run. Includes machine-dependent latency."""
    default = PROVIDERS[suite.provider_key]
    return {
        "average_token_reduction_pct": suite.avg_token_reduction_pct,
        "overall_token_reduction_pct": suite.overall_token_reduction_pct,
        "average_transformation_ms": suite.avg_transformation_ms,
        "cost_reduction_pct": suite.cost_reduction_pct,
        "cache_hit_rate": suite.cache_hit_rate,
        "cost_saved_usd_per_run": estimate_cost(suite.total_tokens_saved, default),
        "top_transformers": [
            {"name": tc.name, "count": tc.count} for tc in suite.top_transformers(limit=5)
        ],
        "most_improved_file_types": [
            {"content_type": s.content_type, "avg_token_reduction_pct": s.avg_token_reduction_pct}
            for s in suite.file_type_stats()
        ],
    }


# -- Human report + README ---------------------------------------------------


def _badge(label: str, value: str, color: str) -> str:
    slug = f"{label}-{value}-{color}".replace(" ", "_").replace("%", "%25")
    return f"![{label}](https://img.shields.io/badge/{slug})"


def readme_block(suite: SuiteResult) -> str:
    """The benchmark section injected into the README between the markers."""
    badges = " ".join(
        [
            _badge("token reduction", f"{suite.avg_token_reduction_pct}%", "brightgreen"),
            _badge("cost reduction", f"{suite.cost_reduction_pct}%", "brightgreen"),
            _badge("cases", str(len(suite.cases)), "blue"),
        ]
    )
    lines = [
        README_START,
        "",
        "### Benchmarks",
        "",
        "_Generated by `python -m benchmarks.coding` — do not edit by hand._",
        "",
        badges,
        "",
        f"Across {len(suite.cases)} realistic coding-assistant requests, Zibbo cut input "
        f"tokens by **{suite.avg_token_reduction_pct}% on average** "
        f"({suite.overall_token_reduction_pct}% overall). Reductions are large on HTML and "
        "verbose JSON, and near zero on source code — exactly as intended.",
        "",
        "| File type | Cases | Avg token reduction |",
        "|---|---|---|",
    ]
    lines += [
        f"| {stat.content_type} | {stat.cases} | {stat.avg_token_reduction_pct}% |"
        for stat in suite.file_type_stats()
    ]
    lines += [
        "",
        "Full methodology, datasets, and where Zibbo does *not* help: "
        "[docs/BENCHMARKS.md](docs/BENCHMARKS.md).",
        "",
        README_END,
    ]
    return "\n".join(lines)


def render_markdown(suite: SuiteResult, *, tokenizer_exact: bool) -> str:
    counter = "exact (tiktoken)" if tokenizer_exact else "approximate (offline heuristic)"
    out = [
        "# Zibbo benchmark report",
        "",
        "_Generated by `python -m benchmarks.coding`. Reproducible; regenerate rather than edit._",
        "",
        "## Summary",
        "",
        f"- Cases: **{len(suite.cases)}** across {len({c.project for c in suite.cases})} projects",
        f"- Average token reduction: **{suite.avg_token_reduction_pct}%** "
        f"(overall {suite.overall_token_reduction_pct}%)",
        f"- Tokens: {suite.total_original_tokens:,} → {suite.total_optimized_tokens:,} "
        f"(**{suite.total_tokens_saved:,}** saved)",
        f"- Cache: a repeated request hits the cache for "
        f"{round(suite.cache_hit_rate * 100)}% of cases",
        f"- Token counter: {counter}",
        f"- Avg transformation time: {suite.avg_transformation_ms} ms _(machine-dependent)_",
        "",
        "## Estimated cost per run, by provider",
        "",
        "| Provider | Model | $/Mtok | Before | After | Saved |",
        "|---|---|---|---|---|---|",
    ]
    for provider in PROVIDERS.values():
        before = estimate_cost(suite.total_original_tokens, provider)
        after = estimate_cost(suite.total_optimized_tokens, provider)
        out.append(
            f"| {provider.label} | {provider.model} | "
            f"${provider.usd_per_million_input_tokens:.2f} | ${before:.6f} | ${after:.6f} | "
            f"${round(before - after, 6):.6f} |"
        )
    out += [
        "",
        "_Cost is an estimate: tokens x published input list price. Not a bill._",
        "",
        "## Per case",
        "",
        "| Case | Project | Scenario | Type | Tokens (before → after) | Reduction |",
        "|---|---|---|---|---|---|",
    ]
    for case in sorted(suite.cases, key=lambda c: c.case_id):
        out.append(
            f"| {case.case_id} | {case.project} | {case.scenario} | {case.content_type} | "
            f"{case.original_tokens:,} → {case.optimized_tokens:,} | {case.token_reduction_pct}% |"
        )
    out += [
        "",
        "## Most improved file types",
        "",
        "| File type | Cases | Avg token reduction |",
        "|---|---|---|",
    ]
    out += [
        f"| {s.content_type} | {s.cases} | {s.avg_token_reduction_pct}% |"
        for s in suite.file_type_stats()
    ]
    out += ["", ""]
    return "\n".join(out)


def update_readme(suite: SuiteResult, readme: Path = README) -> bool:
    """Replace the benchmark block in the README. Returns whether the file changed."""
    if not readme.exists():
        return False
    text = readme.read_text(encoding="utf-8")
    block = readme_block(suite)
    if README_START in text and README_END in text:
        before = text[: text.index(README_START)]
        after = text[text.index(README_END) + len(README_END) :]
        updated = f"{before}{block}{after}"
    else:  # append a fresh section
        updated = f"{text.rstrip()}\n\n{block}\n"
    if updated != text:
        readme.write_text(updated, encoding="utf-8", newline="\n")
        return True
    return False


# -- Orchestration -----------------------------------------------------------


def generate(
    out_dir: Path = RESULTS_DIR,
    *,
    provider_key: str = "openai",
    project: str | None = None,
    write_readme: bool = False,
) -> SuiteResult:
    """Run the suite and write every artifact. Returns the suite for the caller."""
    suite = run_suite(provider_key, project=project)
    exact = tokenizer_is_exact(suite.model)

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "results.json", results_document(suite, tokenizer_exact=exact))
    (out_dir / "summary.csv").write_text(summary_csv(suite), encoding="utf-8", newline="\n")
    (out_dir / "report.md").write_text(
        render_markdown(suite, tokenizer_exact=exact), encoding="utf-8", newline="\n"
    )
    _write_json(out_dir / "website-stats.json", website_stats(suite))

    if write_readme and project is None:
        update_readme(suite)
    return suite


def _write_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )
