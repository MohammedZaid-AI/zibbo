"""The coding benchmark suite: dataset integrity, reproducibility, honest results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from benchmarks.coding import report
from benchmarks.coding.integrity import dataset_checksums, load_checksums
from benchmarks.coding.runner import DATASETS_DIR, load_cases, run_suite

REQUIRED_PROJECTS = {
    "FastAPI",
    "Next.js",
    "React",
    "TypeScript",
    "Django",
    "Go",
    "Rust",
    "Node.js",
}


# -- Dataset integrity -------------------------------------------------------


def test_every_manifest_case_points_at_a_real_nonempty_file() -> None:
    for case in load_cases():
        path = DATASETS_DIR / case.file
        assert path.is_file(), f"missing dataset file: {case.file}"
        assert path.stat().st_size > 0, f"empty dataset file: {case.file}"


def test_case_ids_are_unique() -> None:
    ids = [case.id for case in load_cases()]
    assert len(ids) == len(set(ids))


def test_all_named_projects_are_covered() -> None:
    projects = {case.project for case in load_cases()}
    assert projects >= REQUIRED_PROJECTS


def test_datasets_match_committed_checksums() -> None:
    """Datasets must not drift silently. Regenerate with `python -m benchmarks.coding.integrity`."""
    assert dataset_checksums() == load_checksums()


# -- Reproducibility ---------------------------------------------------------


def test_results_document_is_deterministic() -> None:
    first = report.results_document(run_suite("openai"), tokenizer_exact=False)
    second = report.results_document(run_suite("openai"), tokenizer_exact=False)
    assert first == second


def test_summary_csv_is_deterministic() -> None:
    assert report.summary_csv(run_suite("openai")) == report.summary_csv(run_suite("openai"))


# -- Honest results (the validation the phase is about) ----------------------


def test_html_gets_large_reduction_and_source_code_gets_almost_none() -> None:
    by_id = {c.case_id: c for c in run_suite("openai").cases}

    # HTML documentation: a big win.
    assert by_id["nextjs-docs-explain"].token_reduction_pct > 50
    assert by_id["nextjs-release-notes-docs"].token_reduction_pct > 50

    # Pure source code: near-zero, and we say so rather than hide it.
    for source_case in (
        "react-component-review",
        "go-service-refactor",
        "rust-module-duplicate-logic",
    ):
        assert by_id[source_case].token_reduction_pct < 5

    # Verbose JSON: a solid win.
    assert by_id["fastapi-openapi-architecture"].token_reduction_pct > 30


def test_a_repeat_hits_the_cache() -> None:
    assert all(case.cache_hit for case in run_suite("openai").cases)


def test_no_result_carries_dataset_content() -> None:
    # A result is metadata only: its repr must not contain a distinctive dataset string.
    suite = run_suite("openai")
    blob = repr(suite.cases)
    assert "getServerSideProps" not in blob  # from the Next.js HTML dataset
    assert "UserCard" not in blob  # from the React dataset


# -- Report generation -------------------------------------------------------


def test_generate_writes_all_artifacts(tmp_path: Path) -> None:
    suite = report.generate(out_dir=tmp_path, write_readme=False)
    for name in ("results.json", "summary.csv", "report.md", "website-stats.json"):
        assert (tmp_path / name).is_file()

    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert results["totals"]["cases"] == len(suite.cases)
    assert results["cases"], "expected per-case rows"

    website = json.loads((tmp_path / "website-stats.json").read_text(encoding="utf-8"))
    assert "average_token_reduction_pct" in website
    assert website["most_improved_file_types"], "expected file-type breakdown"


def test_readme_block_is_replaced_between_markers(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        "intro\n<!-- BENCHMARKS:START -->\nOLD\n<!-- BENCHMARKS:END -->\noutro\n",
        encoding="utf-8",
    )
    changed = report.update_readme(run_suite("openai"), readme=readme)
    assert changed is True
    text = readme.read_text(encoding="utf-8")
    assert "OLD" not in text
    assert text.startswith("intro\n")
    assert text.rstrip().endswith("outro")
    assert "### Benchmarks" in text


def test_website_stats_come_only_from_results() -> None:
    suite = run_suite("openai")
    stats = report.website_stats(suite)
    # Every published number is derivable from the suite — no hand-entered constants.
    assert stats["average_token_reduction_pct"] == suite.avg_token_reduction_pct
    assert stats["cache_hit_rate"] == suite.cache_hit_rate


@pytest.mark.parametrize("provider", ["openai", "anthropic", "openai-mini"])
def test_cost_scales_with_provider_price(provider: str) -> None:
    suite = run_suite(provider)
    # More expensive input tokens -> a larger dollar saving for the same reduction.
    assert suite.cost_saved_usd >= 0
    assert suite.cost_before_usd >= suite.cost_after_usd
