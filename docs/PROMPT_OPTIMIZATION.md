# Prompt optimization

AI coding assistants receive long, human-written prompts, and humans repeat themselves.
The same instruction gets pasted twice. A `Requirements:` section is copied and edited.
The same bullet ends up under two copies of the same heading. You pay for every repeated
token.

The prompt optimizer removes that **exact** redundancy and nothing else. It is a
deterministic transformer, exactly like the HTML and JSON transformers: same input, same
output, on every machine, forever. There is no model in the loop.

```
Same intent. Fewer tokens. No AI in the loop.
```

It is **off by default** and provider-agnostic — it is not special-cased for Claude Code,
Codex, or anything else.

## What it removes

All of these are keep-first, so ordering never changes:

| Removed | Example |
|---|---|
| Exact-duplicate blocks | a paragraph pasted twice |
| Byte-identical fenced code blocks | the same ```` ```python ```` block twice |
| Byte-identical Markdown tables | a spec table pasted twice |
| An isolated repeated heading | a second `Requirements:` |
| Duplicate list items **under the same heading** | the same bullet under two `Requirements:` sections |
| A sentence immediately repeated inside a paragraph | "Do this. Do this." |

## What it will never do

By construction, not by policy — every operation is exact-string removal of a *later*
duplicate:

- paraphrase or summarize
- replace a word with a synonym
- infer intent
- reorder anything
- correct grammar or spelling
- shorten an explanation
- touch the inside of a code fence or inline code
- remove an example, a stack trace, or an error message

The safety property that makes duplicate list items safe is **section scope**: an
identical bullet under a *different* heading means something different and is kept.

```
Frontend tasks:          Frontend tasks:
- Add a button.          - Add a button.
                    →
Backend tasks:           Backend tasks:
- Add a button.          - Add a button.     ← kept: different section
```

## A worked example

```
Fix this bug.                          Fix this bug.

Requirements:                          Requirements:

- Don't modify authentication.         - Don't modify authentication.
- Don't modify CSS.            →        - Don't modify CSS.
- Return complete files.               - Return complete files.

Requirements:

- Don't modify CSS.
- Return complete files.
```

The second `Requirements:` block is a byte-identical duplicate and is dropped; its two
bullets already appeared under the first `Requirements:` and are dropped. The first
section, and the order of everything, is untouched.

> **A note on "semantic" duplication.** A human reading
> `Don't touch auth` / `Don't change login` / `Authentication must remain the same`
> can see they mean one thing. Collapsing them requires *inferring intent* — the one
> thing a deterministic, no-AI transformer must not do. The prompt optimizer removes
> exact duplicates only; near-duplicates are left exactly as written.

## Detection

Content is classified as an optimizable **PROMPT** only when **all** hold:

1. it is plain text or Markdown (JSON, HTML, XML are claimed by their own detectors at
   higher confidence and are never reclassified);
2. it is longer than `ZIBBO_PROMPT_OPTIMIZATION_MIN_CHARS` (default **240** — sized to
   admit a realistic coding prompt; the duplicate ratio below, not length, is the real
   guard against reshaping ordinary prose);
3. a fraction of its non-blank lines above
   `ZIBBO_PROMPT_OPTIMIZATION_MIN_DUPLICATE_RATIO` (default **0.15**) are exact
   duplicates;
4. it does not read as source code, a log, or a stack trace.

Everything else — short prompts, unique prose, code, logs — is handled exactly as before
by the plain-text transformer. Even a misclassification is harmless: the transformer only
removes exact duplicates, and the pipeline's never-grow guard forwards the original bytes
if nothing was saved.

## Configuration

Off by default. Enable it per deployment:

```bash
ZIBBO_PROMPT_OPTIMIZATION=true
ZIBBO_PROMPT_OPTIMIZATION_MIN_CHARS=240
ZIBBO_PROMPT_OPTIMIZATION_MIN_DUPLICATE_RATIO=0.15
```

Or toggle it live, without a restart:

```bash
zibbo enable prompt
zibbo disable prompt
zibbo doctor            # shows the prompt optimizer's status
```

`zibbo enable prompt` registers the transformer and its detector together, so detection
and transformation can never disagree, and the transformation cache namespaces the two
states apart — a result computed while the feature was off is never reused while it is on.

## Pipeline integration

The prompt optimizer obeys every existing invariant, with no special pipeline logic:

- **deterministic** and **idempotent** — `T(T(x)) == T(x)`;
- **never-grow** — the shared guard forwards the original bytes if de-duplication does not
  save tokens;
- **cache**, **analytics**, **benchmark**, **plugin**, **explain**, and
  **transformation-report** support all come for free, because it is an ordinary
  `Transformer` returning text plus the names of the steps it applied.

## Explaining a reduction

`zibbo explain` (and `/zibbo:explain`) renders the steps for the last request:

```
✓ Removed duplicate instructions
✓ Removed duplicate instruction block

Saved 469 tokens
```

## Benchmarks

Realistic Claude Code prompts — repeated instructions, repeated constraints, duplicate
Project rules / Context sections, a preserved code block and stack trace — are generated
from a fixed seed and measured through the real pipeline:

```bash
python -m benchmarks.prompts
```

Never a claim without a measurement: the runner reports bytes and tokens before/after,
median latency, and the cache outcome on a repeated run.
