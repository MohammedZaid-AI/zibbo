# Benchmarks

This suite measures what Zibbo does to the requests an AI coding assistant actually
sends — READMEs, source files, HTML docs, error logs, JSON responses — and produces
reproducible evidence. It is deliberately honest: it includes cases where Zibbo saves
almost nothing, and it never rounds those away.

Run it:

```bash
python -m benchmarks.coding            # full suite; writes benchmarks/results/ and the README block
python -m benchmarks.coding --project FastAPI
python -m benchmarks.coding --provider anthropic --print
# or, if the gateway is installed:  zibbo benchmark --suite
```

## Methodology

Each dataset case is a realistic request. The suite runs it through the **real Zibbo
pipeline, in-process** — the same detection, transformers, and cache a live request uses —
and records the result. No provider is contacted and no network is used.

* **WITHOUT Zibbo** is the original request: its bytes and the tokens the provider would
  have received.
* **WITH Zibbo** is the optimized request the gateway would forward.
* Each case is run **twice**: once cold (the real cost of a first request) and once warm,
  to confirm an identical repeat is served from the cache.

We do **not** call the provider. Token reduction and cost are exact functions of the
content and the tokenizer; they do not need a network round-trip, and adding one would
make the results non-reproducible and cost money. End-to-end latency to a provider is
dominated by the network and the model, neither of which Zibbo changes, so the latency we
report is the gateway's own transformation time — the only part it is responsible for.

### Measurements collected

Per case: original/optimized bytes, original/optimized tokens, token reduction %, bytes
removed, cache hit-on-repeat, transformer steps applied, and cold transformation time.
Per run: totals, averages, per-file-type reduction, top transformers, and estimated cost
for each provider. All of it is metadata — **no dataset content ever enters a result**, so
the reports are safe to publish.

### Cost estimation

Cost is `tokens × published input list price` (see `benchmarks/coding/pricing.py`). It is
an **estimate**, not a bill: real cost depends on the model, output tokens, and any
discount. Prices are updated alongside the datasets, never silently.

## Datasets

`benchmarks/coding/datasets/`, indexed by `manifest.json`. Each entry names a project, a
scenario, a file, a media type, and a description.

These are **representative samples, not live repository clones.** Cloning FastAPI, Next.js,
React, and the rest would be large, non-deterministic, and a licensing tangle. Instead each
file is a realistic instance of what a developer pastes or attaches for that workflow — an
OpenAPI schema, a doc page saved as HTML, a component, a traceback. They are small so the
suite runs in a second; the *ratios* they demonstrate hold at scale, and larger inputs
generally reduce *more*, not less (more chrome to strip).

Adding a project or scenario is one file plus one manifest entry. The datasets are checksum-
tested (see Testing), so a change is always intentional.

Current coverage: FastAPI, Next.js, React, TypeScript, Django, Go, Rust, Node.js —
across explain / review / refactor / find-duplicates / generate-docs / analyze-architecture
/ explain-error-log / review-PR / understand-README.

## Reproducing results

```bash
python -m benchmarks.coding
```

`benchmarks/results/results.json` and `summary.csv` are **deterministic**: run the suite
twice on the same machine and they are byte-for-byte identical. They contain no timestamp
and no wall-clock timing for exactly this reason.

`report.md` and `website-stats.json` additionally include transformation time, which **is**
machine-dependent; treat those timing figures as indicative, not reproducible.

### Token counter

Token counts come from `tiktoken` when its encodings are available, and from an offline
heuristic counter when they are not (for example, behind a TLS-inspecting proxy that blocks
the encoding download). The reports state which was used (`tokenizer_exact`). The heuristic
is deterministic but approximate; it tends to **understate** the savings on HTML and base64,
so heuristic numbers are conservative. For headline figures, run where `tiktoken` can load
its encodings.

## Hardware and software

Token reduction, bytes, and transformer choices are hardware-independent. Only the timing
figures depend on the machine. Each generated `report.md` records the token counter used;
record your Python version and CPU alongside any timing you publish. Software versions are
pinned in `pyproject.toml` (Zibbo, `tiktoken`, `lxml`).

## Interpretation — where Zibbo helps, and where it does not

Zibbo removes *structural noise*. Its benefit is proportional to how much of that a request
contains.

**Large benefit** — HTML documentation and pages (scripts, styles, nav, cookie banners,
ads stripped; converted to Markdown): **60–80%** fewer tokens. Verbose/pretty-printed JSON
(minified): **40–55%**.

**Little or no benefit — and this is by design:**

* **Pure source code** (`.tsx`, `.go`, `.rs`, `.py`). Whitespace is meaning, not noise, so
  Zibbo touches almost nothing: **~0.3%**. If your workflow is mostly reading code, Zibbo
  will not move your token bill much.
* **Already-clean Markdown** (a tidy README): **~0.5%**. There is nothing to strip.
* **Tiny JSON** (a small config): the reduction % can look fine, but the absolute saving is
  a few dozen tokens — not worth reasoning about.
* **Small prompts** generally: the overhead is never worth discussing below a few hundred
  tokens.

The suite exists to help you decide honestly. If your assistant mostly pastes web docs,
API responses, logs, and PDFs, Zibbo pays for itself. If it mostly reads source files, the
savings are real but small — and the benchmark will tell you so rather than hide it.

## Limitations

* Datasets are representative samples, not full repositories (see above).
* Cost figures are list-price estimates, not bills.
* Timing is machine-dependent and excluded from the reproducibility guarantee.
* The suite measures request-body optimization; it does not measure provider latency,
  which Zibbo does not change.
* Binary document extraction (PDF/DOCX) is benchmarked separately in
  `benchmarks/documents.py`; this suite covers the text/HTML/JSON/code requests an assistant
  sends inline.
