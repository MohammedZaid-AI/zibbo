# Zibbo v0.1.1

First public release. Zibbo is deterministic context optimization for AI coding
assistants: it removes structural noise — HTML chrome, JSON whitespace, log
boilerplate — from each request before it reaches the model, so you send fewer
tokens with the same information. No second model is involved, and it runs
locally, forwarding traffic on your own API key.

## Why deterministic?

Zibbo optimizes with rule-based transformations and no second model. A summarizer
LLM would make probabilistic choices about what to drop — trading a token bill for
a correctness risk, and giving different output on different runs. Rule-based
transformations give the same output for the same input, on every machine. That
makes them reproducible: you can run the benchmark and get identical numbers, and
see exactly which steps ran on a request (`zibbo explain`). Reproducibility and
explainability are what let you trust an automated change to a prompt you are
paying for — you verify it instead of taking it on faith.

## The problem

AI coding assistants routinely send whole HTML pages, pretty-printed JSON, stack
traces, and PDFs to the model. Much of that is scripts, nav bars, indentation, and
base64 — tokens you pay for that carry no information. Zibbo strips that noise
before the request is sent.

## Design

- **No LLM in the loop.** Every transformation is a deterministic function of the
  input — the same bytes in produce the same bytes out, on every machine. There is
  no summarizer model making probabilistic choices about what to drop.
- **Idempotent.** `pipeline(pipeline(x)) == pipeline(x)`.
- **Never-grow.** A transformation that would increase the token count is discarded
  and the original bytes are forwarded.
- **Source code is not reformatted.** Whitespace is meaning in code, so Zibbo leaves
  it alone; in the benchmark suite, source files come back at 0% — and it reports
  that rather than hiding it.
- **Content-blind logging.** No request body enters a log line, a statistic, or a
  benchmark result.

Each of these is covered by property tests in the suite.

## What it does

- HTML → Markdown (scripts, styles, nav, ads removed), JSON minification, whitespace
  normalization, and PDF/DOCX text extraction from base64 attachments.
- Providers: OpenAI- and Anthropic-compatible endpoints; Groq, Mistral, and Ollama
  via the OpenAI-compatible path. Your credentials are forwarded unchanged.
- Optional prompt de-duplication (removes exact-duplicate instruction blocks). Off by
  default.

## CLI

Measure before you route — benchmarking runs the pipeline locally and forwards
nothing to a provider:

```
zibbo start                                     # start the local gateway
zibbo benchmark --content "$(cat page.html)"    # measure — nothing is sent upstream
zibbo explain                                   # what was removed, and why
zibbo stop                                      # stop, and restore your settings exactly
```

## Benchmarks

Reproducible with `zibbo benchmark --suite`. The suite runs the real pipeline
in-process on a fixed corpus, contacts no provider, and writes deterministic results
(byte-identical across runs). On the current 10-case suite:

- **28%** average token reduction per request, **40%** overall by token volume
- HTML documentation **~77%**, JSON **~43%**, source code **0%**

Method and per-case results: [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

## Install

```
pipx install git+https://github.com/MohammedZaid-AI/zibbo
```

Claude Code plugin: `/plugin marketplace add MohammedZaid-AI/zibbo`.

Requires Python 3.11+.

## Scope

Zibbo helps in proportion to how much structural noise a request contains: a large win
on documentation, HTML, verbose JSON, and logs; roughly nothing on pure source code —
by design. The benchmark will tell you which case you're in.

## License

Apache-2.0.
