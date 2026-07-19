<div align="center">

<img src="docs/assets/avatar.png" alt="Zibbo" width="140" height="140" />

# Zibbo

**A deterministic context optimization engine for AI coding assistants.**

Zibbo removes structural noise — HTML chrome, JSON whitespace, log boilerplate —
from each request before it reaches the model. The information is preserved; the
token count drops. No second model is involved, and the same input always
produces the same output. It runs locally, forwarding traffic on your own API key.

[![CI](https://github.com/MohammedZaid-AI/zibbo/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammedZaid-AI/zibbo/actions/workflows/ci.yml)
[![HTML docs −77% tokens](https://img.shields.io/badge/HTML_docs-%E2%88%9277%25_tokens-brightgreen)](docs/BENCHMARKS.md)
[![no LLM in the loop](https://img.shields.io/badge/LLM_in_the_loop-none-blue)](#what-zibbo-never-does)
[![deterministic](https://img.shields.io/badge/transformations-deterministic-black)](#trust)
[![license](https://img.shields.io/badge/license-Apache_2.0-black)](LICENSE)

</div>

---

## What it does

AI coding assistants send whole HTML pages, pretty-printed JSON, stack traces,
and PDFs to the model. Much of that is scripts, nav bars, indentation, and
base64 — tokens you pay for that carry no information.

Zibbo removes that structural noise on your machine, then forwards the request
to the real provider on your own API key. It converts HTML to Markdown,
minifies JSON, and normalizes whitespace. It does not summarize, paraphrase, or
reorder anything, and it calls no model to do the work.

If a request wouldn't get smaller, Zibbo forwards the original bytes unchanged.

---

## Before / after

A documentation page pasted into an assistant (`nextjs-docs-explain` in the
benchmark suite):

```
   You send                          Zibbo forwards
   ┌──────────────────────┐          ┌──────────────────────┐
   │ <!DOCTYPE html>…      │          │ # Installing Next.js  │
   │ <script>…analytics…   │  ──────► │                       │
   │ <nav>…</nav>          │          │ Run `npx create-next… │
   │ 939 tokens            │          │ 230 tokens            │
   └──────────────────────┘          └──────────────────────┘
                                         75% fewer tokens
```

Source files are a different story — and Zibbo says so:

```
   main.py (247 tokens)  ──────►  main.py (247 tokens)     0% change
```

---

## How it works

```
   ┌────────────┐      ┌─────────────────────────────┐      ┌───────────┐
   │ Claude Code│      │            Zibbo             │      │  Provider │
   │  / Codex   │ ───► │  detect → transform → verify │ ───► │ Anthropic │
   │  / any SDK │      │  (HTML, JSON, logs, PDFs)    │      │  / OpenAI │
   └────────────┘      └─────────────────────────────┘      └───────────┘
```

A transparent proxy: it optimizes the request body, forwards it on your own
credentials, and streams the provider's response straight back — Zibbo never
touches responses. The provider receives a smaller, equivalent request.

---

## Install

**Claude Code**

```text
/plugin marketplace add MohammedZaid-AI/zibbo
/plugin install zibbo@zibbo
```

**Standalone CLI (any assistant)**

```bash
pipx install git+https://github.com/MohammedZaid-AI/zibbo
```

---

## Measure before you route

See what Zibbo would save on a real file **before** it touches your assistant's
traffic. Benchmarking runs the pipeline locally and forwards nothing to a
provider — your API key is never used.

```bash
zibbo start                                          # start the local gateway
zibbo benchmark --content "$(cat some-page.html)"    # measure — nothing is sent upstream
zibbo stop                                           # stop, and restore your settings exactly
```

```text
Zibbo benchmark  (sample)

  Content type:     html
  Original tokens:  939
  Optimized tokens: 230
  Reduction:        75.5%
```

The routing config `zibbo start` writes only takes effect after you **restart**
your assistant — so this loop never routes real traffic, and `zibbo stop` puts
your settings back exactly. Source code returns ~0%, and it says so. When the
numbers convince you, route for real (below).

---

## Connect

`zibbo connect` runs the gateway and routes Claude Code through it. You do not
edit a settings file by hand.

```bash
zibbo connect
```

```text
✓ Gateway running (http://127.0.0.1:8000)
✓ Claude Code configured  (~/.claude/settings.json)

  Restart Claude Code for routing to take effect.
```

It writes `ANTHROPIC_BASE_URL` into your **global** Claude Code settings
(`~/.claude/settings.json`), which both the CLI and the **VS Code extension** read
regardless of which folder is open — so a shell `export` isn't needed (and doesn't
reach the VS Code extension anyway). Use `zibbo connect --project` to scope routing
to the current workspace instead, and `zibbo disconnect` to undo it. `zibbo start`
does the same thing.

For any other assistant, set its base URL to Zibbo — `…/v1` for OpenAI-style
clients, `…/anthropic` for Anthropic-style. Your API key is forwarded untouched;
requests bill your own account.

> **Not routed after connecting?** Restart your assistant (settings are read at
> launch), then check `zibbo status` → **Routing: Observed → Active**. If it's still
> off, run `zibbo doctor --fix`.

---

## Verify what it changed

Every response states what Zibbo did, in headers:

```text
x-zibbo-optimization: applied
x-zibbo-tokens-saved: 709
```

Inspect the last request, or run any file through the pipeline yourself:

```text
$ zibbo explain

Last request

  Content type:      HTML
  Original tokens:   939
  Optimized tokens:  230
  Saved:             709 (75.5%)

  Transformations applied:
    ✓ Removed scripts
    ✓ Removed navigation
    ✓ Converted HTML to Markdown
```

```bash
zibbo benchmark --content "$(cat some-page.html)"
```

---

## What is optimized

| Input | What Zibbo removes | Reduction |
|---|---|:---:|
| **HTML** pages & docs | scripts, styles, nav, ads → Markdown | **77%** |
| **JSON** API responses | pretty-print whitespace and escapes → minified | **43%** |
| **PDF / DOCX** attachments | base64 payload → extracted text | varies |
| **Logs / prose** | trailing whitespace, redundant blank lines | small |
| **Source code** | nothing — code is not reformatted | **0%** |

Numbers are averages by content type from a 10-case suite: **28% average
reduction per request, 40% by token volume**. Reproduce them with `zibbo
benchmark --suite`; method and per-case results in
[docs/BENCHMARKS.md](docs/BENCHMARKS.md).

## What Zibbo never does

- It does not call another model. There is no LLM in the optimization path.
- It does not summarize, paraphrase, or reorder your prompts.
- It does not reformat or reindent source code; inline whitespace is preserved.
- It does not make a request larger. If optimizing wouldn't help, it forwards the original bytes.
- It does not store request contents. Logs and statistics are metadata only.

---

## Stop

```bash
zibbo stop
```

```text
✓ Gateway stopped
✓ Claude Code restored  (removed Zibbo routing)

  Restart Claude Code to return to the normal endpoint.
```

`zibbo stop` restores the endpoint from a backup written at `start`, including
any base URL you had configured before.

---

## Optional: prompt de-duplication

Long, hand-written prompts often repeat themselves — an instruction pasted
twice, a `Requirements:` section copied and edited. Zibbo can remove that exact
redundancy: duplicate instruction blocks, repeated sections, and duplicate
bullets under the same heading. It does not paraphrase, reorder, or touch code,
examples, or stack traces.

Off by default:

```bash
zibbo enable prompt      # live, no restart   (or set ZIBBO_PROMPT_OPTIMIZATION=true)
zibbo disable prompt
```

Behaviour and guarantees: [docs/PROMPT_OPTIMIZATION.md](docs/PROMPT_OPTIMIZATION.md).

---

## Commands

```bash
zibbo start       # start the gateway and route your assistant through it
zibbo stop        # stop the gateway and restore your assistant's endpoint
zibbo status      # what is running, what is routed, current endpoint
zibbo stats       # tokens saved, today and all-time
zibbo explain     # what the last request had removed, and why
zibbo benchmark   # measure the savings on any file, or the built-in suite
zibbo doctor      # diagnose setup problems; each check names its fix
```

Inside Claude Code the same views are `/zibbo:zibbo`, `/zibbo:stats`, and
`/zibbo:doctor`.

---

## Trust

Four guarantees, each covered by property tests in the suite:

- **Deterministic** — no model, no randomness. Output is a pure function of input.
- **Idempotent** — `pipeline(pipeline(x)) == pipeline(x)`. Running it twice changes nothing.
- **Never-grow** — a transformation that produces more tokens is discarded and the original forwarded.
- **Content-blind logging** — no request body enters a log line, a statistic, or a benchmark result.

---

## Architecture

A request is classified by its content (not its declared type), routed to the
transformer that handles it, transformed, checked against the never-grow
guarantee, cached, and forwarded. Providers, transformers, caching, and plugins
are pluggable. Full internals: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## License

[Apache-2.0](LICENSE).
