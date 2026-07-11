# Transformation cache

Optimizing a request is deterministic: the same document, run through the same
extractors with the same options, always produces the same Markdown. So doing it twice
is waste. The transformation cache does it once — hash the original content, look it up,
and on a hit reuse the stored result instead of extracting, transforming, and token-
counting all over again.

This caches **transformation outputs**, not responses. The provider is still called on
every request; what is reused is the work the gateway does *before* the call.

## What is cached, and what is never cached

A cache entry is the content-derived half of a transformation result:

* the transformed content (extracted Markdown, cleaned text)
* the detected content type and the transformer name and version that produced it
* token counts before and after, and byte sizes before and after
* the cold execution time — what the transformation cost the first time

Four things are **never** cached, and the design is built around these lines:

* **Failed transformations.** An extraction that returns nothing — an encrypted PDF, a
  corrupt DOCX, a scanned page with no text — is not stored. It might succeed on a later
  request or a fixed extractor, and a cached failure would mask that. (A *deterministic
  no-op* — extraction succeeded but the never-grow guard kept the original because the
  Markdown wasn't cheaper — **is** cached; it is a completed result, not a failure.)
* **Partial transformations.** A transformer that raised mid-way returns `None`; there
  is nothing complete to store.
* **Provider responses.** Only inputs the gateway transforms are cached. Response bodies
  are never read, never cached. See [COMPATIBILITY.md](COMPATIBILITY.md).
* **Non-deterministic anything.** There is none in the optimization path by design — no
  model, no clock, no randomness — which is the whole reason a cache is safe here.

## Content hashing

The identity of a piece of content is `SHA-256` of its bytes, computed **before**
transformation. For a document that is the decoded file; for a text segment it is the
UTF-8 of the string. The hash identifies the *input* exactly — two requests carrying the
same PDF hash identically regardless of anything around them.

## Cache keys and invalidation

The hash alone is not the key. The output of a transformation depends on more than its
input, and the key commits to all of it. Change any component and the digest changes, so
a stale result is never served — invalidation is not an operation, it is a consequence of
the key.

| Key component | Why it invalidates |
|---|---|
| **content hash** | different input, different output |
| **transformer version** | a fingerprint of every applicable transformer's name and version; bump one and its entries retire |
| **gateway version** | a gateway upgrade may change transformation behaviour wholesale |
| **options fingerprint** | a digest of the options that shape output (`html_preserve_links`, `text_*`, `documents_*`, …); flip one and cached output is wrong |
| **tokenizer encoding** | token counts are stored, and they are specific to an encoding |
| **qualifier** (documents) | declared media type and filename, which steer format detection |

The "transformer version" is a fingerprint over the *whole registry*, not the single
transformer that ran. This is deliberate: which transformer runs is itself a
deterministic function of the content, so a hit legitimately skips detection, selection,
transformation and token counting together — and any change to the set of transformers
(a plugin added, a version bumped) must retire the affected entries. It over-reaches
slightly — bumping the PDF extractor also retires DOCX entries — but extractor bumps are
rare and correctness is worth more than the extra cold runs.

Bump a transformer by incrementing its `version` class attribute:

```python
class HtmlTransformer(Transformer):
    name = "html"
    version = "2"   # was "1"; every cached HTML result is now invalidated
```

## Storage backends

A backend is a byte store keyed by a string, with per-key expiry — nothing more. That
small contract is what makes them interchangeable.

| Backend | Scope | Use |
|---|---|---|
| **in-memory** (default) | one process | single replica; tests; zero dependencies |
| **Redis** | shared across replicas, survives restart | horizontal scale |
| PostgreSQL, S3 | — | designed for, not yet implemented — each is one class |

**In-memory** is a thread-safe LRU bounded on two axes — entry count *and* total bytes,
because a cache of extracted 100-page PDFs is bounded by neither alone. It is consulted
from the same worker thread the transformation already runs on, so it adds no event-loop
work.

**Redis** stores the same bytes under a namespaced key. It is defensive to a fault: the
`redis` package is imported lazily (absent unless you use this backend), and *every*
Redis call is wrapped so that a missing package, a down server, a timeout, or a reset
degrades to a cache miss. **A cache outage slows the gateway; it never breaks it.** A
Redis backend also registers a readiness probe — a *degraded* one, since an unreachable
cache should not pull the pod from the load balancer.

The backend is synchronous on purpose. The cache is consulted on the same (often worker)
thread as the transformation it guards, so a blocking Redis round-trip does not stall the
event loop for the large payloads that dominate cache value. The trade-off: a *small*
payload transformed inline pays the round-trip on the event-loop thread. If that matters
for a Redis deployment, lower `LLMGATEWAY_OPTIMIZATION_OFFLOAD_THRESHOLD_BYTES` so more
work — and its cache lookup — moves to the pool.

## The request flow

```
request → hash content → look up → HIT  → reuse stored output → forward
                              └────→ MISS → transform → store → forward
```

A hit rebuilds the transformation result from the stored entry and replays the same
in-place substitution a fresh transformation would have done, adding back only `origin`
(where this request's segment lived). A miss transforms as before, then stores — but only
if the result is cacheable by the rules above.

Every proxied response carries `x-llmgateway-cache`: `hit` (every segment reused), `miss`
(none), or `partial` (a mix). `GET /internal/cache` reports the running counters —
hits, misses, stores, corrupted, and the hit rate.

## Corrupted entries

A stored value that will not deserialize — truncated, adversarial, or written under an
older schema — is treated as a miss, dropped from the store so it cannot poison every
future lookup, and counted under `corrupted`. The transformation simply runs. A
property test fires thousands of random byte strings at the deserializer and asserts it
never raises.

## Safety summary

* A cache failure is invisible to the caller — degrades to a miss, never an error.
* A corrupt entry self-heals — dropped and recomputed.
* Failed and partial transformations are never stored.
* Provider responses are never touched.
* The cached output is byte-identical to what a cold transformation would have produced,
  because the key commits to every input that could change it.

## Benchmarks

`python -m benchmarks.cache`, reproducible from a fixed seed. Cold is the first request
(transform + store); warm is the median of the reuses. Figures below use the offline
heuristic tokenizer (this machine cannot reach tiktoken's CDN); absolute times vary by
machine, the ratio does not.

| Workload | Cold | Warm | Speed-up | CPU saved |
|---|---|---|---|---|
| HTML page | 5.6 ms | 0.34 ms | 17× | 94% |
| 10-page PDF | 1,214 ms | 0.31 ms | ~3,900× | ~100% |
| 100-page PDF | 7,861 ms | 1.70 ms | ~4,600× | ~100% |

The PDF numbers are dramatic because a warm hit skips pdfplumber entirely — the extraction
that Phase 7 measured at hundreds of MiB and several seconds becomes a hash and a dict
lookup. Repeated uploads of the same document — a contract re-sent across a conversation,
a spec pasted into every message — are where this pays off most.

## Configuration

| Variable | Effect |
|---|---|
| `LLMGATEWAY_CACHE_ENABLED` | Master switch. `true` by default; when off the cache is a wired no-op. |
| `LLMGATEWAY_CACHE_BACKEND` | `memory` or `redis`. |
| `LLMGATEWAY_REDIS_URL` | Connection URL for the Redis backend. |
| `LLMGATEWAY_CACHE_TTL_SECONDS` | Entry lifetime; `0` = no expiry. Deterministic output does not go stale. |
| `LLMGATEWAY_CACHE_MAX_ENTRIES` | In-memory LRU cap (entries). |
| `LLMGATEWAY_CACHE_MAX_BYTES` | In-memory LRU cap (bytes). |
| `LLMGATEWAY_CACHE_REDIS_PREFIX` | Key namespace, so one Redis serves several deployments. |

## Limitations

* **No negative caching.** A "no transformer applies" or "extraction failed" outcome is
  recomputed every time. This is intentional — it keeps failures out of the cache — but
  it means a repeatedly-uploaded scanned PDF re-runs pdfplumber each time.
* **Registry-wide document invalidation.** Bumping one extractor retires every format's
  document entries, not just that format's.
* **Small-payload Redis latency** rides the event loop unless offloading is tuned down
  (see Storage backends).
* **PostgreSQL and S3 backends are designed for but not implemented.**
