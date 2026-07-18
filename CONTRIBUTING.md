# Contributing to Zibbo

Thanks for your interest. Zibbo is a deterministic context optimization engine — the bar
for a change is that it keeps that promise. This guide covers setup, the quality gates, and
the invariants a contribution must not break.

## Development setup

Requires Python 3.11+.

```bash
git clone https://github.com/MohammedZaid-AI/zibbo
cd zibbo
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Run the gateway locally:

```bash
python -m gateway            # http://localhost:8000
```

## Quality gates

Every change must pass all four. CI runs exactly these on Python 3.11 and 3.12:

```bash
ruff check .              # lint
ruff format --check .     # formatting
mypy gateway              # types (strict)
pytest                    # tests
```

`ruff format` (no `--check`) applies formatting locally.

## Invariants — do not break these

Zibbo's value is its guarantees. A change that weakens one will not be merged:

- **Deterministic** — no model, no randomness. Output is a pure function of input.
- **Idempotent** — `pipeline(pipeline(x)) == pipeline(x)`.
- **Never-grow** — a transformation that increases token count is discarded and the original
  forwarded.
- **Content-blind logging** — no request body may enter a log, a statistic, or a benchmark
  result.

These are covered by property tests (`tests/test_*_properties.py`); new transformer behavior
needs matching property coverage.

## Adding a transformer or provider

The pipeline is provider-agnostic and content-routed. A new content transformer is a class
registered in one place; a new provider is one subclass. See
[docs/EXTENDING.md](docs/EXTENDING.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
Prefer plugins (`docs/PLUGIN_DEVELOPMENT.md`) for anything that shouldn't live in core.

## Benchmarks

Results are deterministic and checked in. Regenerate rather than hand-edit:

```bash
python -m benchmarks.coding        # writes benchmarks/results/ and the README block
```

See [docs/BENCHMARKS.md](docs/BENCHMARKS.md) for methodology.

## Pull requests

- Keep changes focused; one concern per PR.
- Explain the *why*, not just the *what*.
- Match the surrounding style — the codebase favors explicit, boring, well-commented code.
- Green CI is required.

## License

By contributing, you agree that your contributions are licensed under the
[Apache-2.0 License](LICENSE).
