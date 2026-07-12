"""``python -m benchmarks.coding`` — run the suite and write the reports.

python -m benchmarks.coding                     # full suite, writes benchmarks/results/
python -m benchmarks.coding --project FastAPI    # one project
python -m benchmarks.coding --provider anthropic # price/count for another provider
python -m benchmarks.coding --readme             # also refresh the README benchmark block
python -m benchmarks.coding --print              # also echo the summary to stdout
"""

from __future__ import annotations

import argparse
import sys

from benchmarks.coding.pricing import PROVIDERS
from benchmarks.coding.report import RESULTS_DIR, generate, render_markdown
from benchmarks.coding.runner import tokenizer_is_exact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="benchmarks.coding", description="Run the Zibbo suite.")
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="openai")
    parser.add_argument("--project", help="limit to one project")
    parser.add_argument(
        "--readme",
        action="store_true",
        help="also inject a generated block into the README (needs the BENCHMARKS markers)",
    )
    parser.add_argument("--print", action="store_true", help="echo the markdown report to stdout")
    args = parser.parse_args(argv)

    suite = generate(
        provider_key=args.provider,
        project=args.project,
        write_readme=args.readme,
    )

    if args.print:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        print(render_markdown(suite, tokenizer_exact=tokenizer_is_exact(suite.model)))
    else:
        print(
            f"Wrote {RESULTS_DIR}/ — {len(suite.cases)} cases, "
            f"{suite.avg_token_reduction_pct}% avg token reduction."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
