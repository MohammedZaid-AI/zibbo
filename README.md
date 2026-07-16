<div align="center">

<img src="docs/assets/avatar.png" alt="Zibbo" width="160" height="160" />

# Zibbo
### Your AI coding assistant wastes tokens. Zibbo takes them back.

Claude Code, Codex, and Gemini CLI send raw HTML, logs, JSON, and PDFs straight
to the model. Zibbo cleans out the noise first — **same answers, up to 75% fewer tokens.**

One command to install. Zero changes to your code.

<br>

[![GitHub stars](https://img.shields.io/github/stars/MohammedZaid-AI/zibbo?style=social)](https://github.com/MohammedZaid-AI/zibbo)
[![token reduction](https://img.shields.io/badge/token_reduction-up_to_75%25-brightgreen)](docs/BENCHMARKS.md)
[![no AI in the loop](https://img.shields.io/badge/AI_in_the_loop-none-blue)](#-what-zibbo-will-never-do)
[![license](https://img.shields.io/badge/license-MIT-black)](LICENSE)

<br>

```text
/plugin marketplace add MohammedZaid-AI/zibbo
```

<br>

![Zibbo demo](docs/assets/demo.gif)

<sub><i>Placeholder — drop a demo GIF at <code>docs/assets/demo.gif</code></i></sub>

</div>

---

## The 15-second version

Claude Code often sends HTML pages, stack traces, JSON blobs, docs, and PDFs
straight to the model.

Most of that is scripts, nav bars, whitespace, and base64 — **and you pay for
every token of it.**

Zibbo strips the noise before the request reaches the model.

Your source code is never touched.

---

## Is Zibbo for me?

**You'll love it if you regularly send:**

| ✅ | |
|---|---|
| ✅ | Documentation & web pages |
| ✅ | HTML |
| ✅ | Swagger / OpenAPI specs |
| ✅ | Stack traces & logs |
| ✅ | API responses |
| ✅ | Large JSON |
| ✅ | PDFs |

**You probably don't need it if you mostly send:**

| ❌ | |
|---|---|
| ❌ | Pure source code |
| ❌ | Tiny prompts |
| ❌ | Already-clean Markdown |

We'd rather tell you the truth than oversell it. Zibbo shines on messy context
and honestly says `~0%` when there's nothing to clean.

---

## Install

Pick your tool. That's the whole setup.

### Claude Code

```text
/plugin marketplace add MohammedZaid-AI/zibbo
/plugin install zibbo@zibbo
```

Then point Claude Code at the gateway:

```bash
export ANTHROPIC_BASE_URL=http://localhost:8000/anthropic
```

A hook launches the gateway for you. Type `/zibbo:zibbo` to watch the savings roll in.

### Codex

```text
codex plugin marketplace add MohammedZaid-AI/zibbo
codex plugin install zibbo
```

### Any other assistant (CLI)

```bash
pip install -e ".[dev]"
python -m gateway            # http://localhost:8000
```

Point your tool's base URL at Zibbo — `…/v1` for OpenAI-style, `…/anthropic`
for Anthropic-style. Your API key is forwarded untouched; requests still bill
your own account.

---

## See it working

One real documentation request through Claude Code:

```text
   Without Zibbo              With Zibbo
   ┌────────────┐            ┌────────────┐
   │  120,000   │            │   74,000   │   ← same answer
   │   tokens   │   ────►    │   tokens   │
   └────────────┘            └────────────┘
                                  ▼
                          ~38% cheaper request
```

![zibbo explain](docs/assets/explain.png)

<sub><i>Placeholder — <code>zibbo explain</code> shows exactly what was removed and how many tokens it saved.</i></sub>

---

## The receipts

Real files, the real pipeline, honest numbers.

| What you're sending | Token reduction |
|---|:---:|
| 📄 HTML documentation | **~75%** |
| 🔩 Verbose JSON APIs | **~49%** |
| 💻 Source code | **~0%** |

That last row is the honest one. Source code goes through **untouched** — so if
your day is mostly code, Zibbo won't change your bill, and it says so. If your
day is docs, API payloads, and logs, it pays for itself immediately.

<div align="center">

**29.9% average token reduction · 41.2% average cost reduction** across a 10-case suite.

</div>

Reproduce every number yourself: `zibbo benchmark --suite` · full method in [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

---

## Optional: prompt de-duplication

Long, hand-written coding prompts repeat themselves — the same instruction pasted twice,
a `Requirements:` section copied and edited. Zibbo can strip that **exact** redundancy,
deterministically, with no model in the loop. It removes exact-duplicate instruction
blocks, repeated sections, and duplicate bullets under the same heading — and never
paraphrases, reorders, or touches code, examples, or stack traces.

It's **off by default**. Turn it on when you want it:

```bash
zibbo enable prompt      # live, no restart   (or set ZIBBO_PROMPT_OPTIMIZATION=true)
zibbo disable prompt
```

Measured on realistic assistant prompts: **17–87% fewer tokens** depending on how
repetitive the prompt is. Full behaviour and guarantees in
[docs/PROMPT_OPTIMIZATION.md](docs/PROMPT_OPTIMIZATION.md); benchmark it with
`python -m benchmarks.prompts`.

---

## Commands

```bash
zibbo status      # is it running? what's enabled?
zibbo stats       # tokens & cost saved, today and all-time
zibbo explain     # why the last request got smaller
zibbo benchmark   # try it on any file and see the savings
```

![zibbo status](docs/assets/status.png)

<sub><i>Placeholder — <code>zibbo status</code>. Full command list: <code>zibbo --help</code>.</i></sub>

Try it on anything, no assistant required:

```bash
zibbo benchmark --content "$(cat some-page.html)"
```

Inside Claude Code, it's all there as `/zibbo:zibbo`, `/zibbo:stats`, `/zibbo:doctor`
(Claude Code namespaces plugin commands, so they start with `/zibbo:`).

---

## 🔒 What Zibbo will never do

This is the part that lets you install without worrying:

- ✓ **Never** rewrites your source code
- ✓ **Never** uses another AI model
- ✓ **Never** summarizes or paraphrases your prompts
- ✓ **Never** makes a request bigger — if cleaning wouldn't help, it forwards the original
- ✓ **Never** stores your prompt contents — logs and stats are metadata only

Same input, same output, every time. No model, no guessing, no surprises.

Every response even tells you what happened:

```text
x-zibbo-optimization: applied
x-zibbo-tokens-saved: 110
```

---

## Architecture

![How Zibbo works](docs/assets/architecture.svg)

Zibbo sits between your assistant and the model. Messy context goes in, clean
Markdown comes out, your code passes straight through.

```text
  Claude Code  ──►  Zibbo  ──►  Model
                  (cleans HTML, JSON,
                   logs, PDFs — never code)
```

Want the internals — providers, transformers, caching, plugins?
→ [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

---

<div align="center">

### Spend your tokens on answers, not noise.

[Install](#install) · [Benchmarks](docs/BENCHMARKS.md) · [Docs](docs/) · [Star on GitHub ⭐](https://github.com/MohammedZaid-AI/zibbo)
</div>
