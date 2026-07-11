# Document transformers

LLMGateway extracts uploaded documents to clean Markdown before they reach the
provider. A PDF, DOCX, CSV or XML embedded in a request is decoded, converted to the
text a model can actually read, and substituted in place — deterministically, with no
model, no summarization, no rewriting.

The saving is large because a base64-encoded document is doubly wasteful: it is
bigger than the raw file, and it tokenizes atrociously, since the tokenizer has no
idea it is looking at a document. Replacing it with prose typically cuts **70–90%** of
its tokens (see Benchmarks).

## Supported formats

| Format | Extractor | What is extracted |
|---|---|---|
| PDF | pdfplumber | headings, paragraphs, lists, tables, reading order; running headers/footers dropped |
| DOCX | python-docx | headings, paragraphs, lists, tables, hyperlinks, in document order |
| CSV / TSV | stdlib `csv` | Markdown table (small) or cleaned CSV (large); empty rows/columns removed |
| XML | lxml | hierarchy as nested headings and lists; attributes preserved |
| HTML | Phase 3 transformer | chrome stripped, converted to Markdown |
| Markdown | text normalizer | normalized only, never rewritten |
| Plain text | text normalizer | whitespace normalized |

Designed for, not yet implemented: **PPTX, XLSX, EPUB, RTF, EML**. Each is one new
extractor module and one registration line; the detector already recognizes PPTX and
XLSX (it just has no extractor for them yet), so it reports them rather than
mis-parsing.

## How a document reaches the gateway

The gateway is a chat proxy, so a document arrives **embedded in a request body**, as
base64 — the way the provider APIs already accept them:

* **Anthropic** — a `document` content block:
  `{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "JVBER..."}}`
* **OpenAI** — a `file` content block with a data URI:
  `{"type": "file", "file": {"filename": "report.pdf", "file_data": "data:application/pdf;base64,JVBER..."}}`

The adapter for the endpoint yields these as *document segments*; the pipeline decodes
each, extracts it, and — only if the Markdown is cheaper — rewrites the block into a
plain `text` block. A prompt-caching directive (`cache_control`) beside the block is
preserved.

Raw file uploads to `/v1/files` are **never** touched: they are binary, and the policy
denies them. "Never modify binary files" is taken literally.

## Architecture

The gateway core contains no document logic. The pipeline calls one method:

```
DocumentService.extract(bytes, media_type=...) -> ExtractionResult
```

```
gateway/documents/
  models.py       DocumentFormat, ExtractionResult
  detection.py    bytes -> format (magic bytes, then media type, then extension)
  base.py         DocumentExtractor ABC — the extract() that never raises
  service.py      detect -> permit -> dispatch, the pipeline's single entry point
  registry.py     format -> extractor
  options.py      enable flags, size limits
  convert/        pure text -> Markdown, shared by extractors and text transformers
    csv_table.py
    xml_tree.py
  extractors/
    pdf.py, docx.py, text_formats.py   (csv, xml, html, markdown, text)
```

Detection follows the brief's order exactly: **magic bytes, then media type, then
extension.** The declared media type is a hint a client can get wrong or lie about;
the bytes cannot. The one hard case is Office formats — DOCX, XLSX and PPTX are all
ZIP archives sharing the `PK\x03\x04` signature — so the archive is opened and its
member names inspected (`word/` → DOCX, `xl/` → XLSX, `ppt/` → PPTX).

`convert/` holds the pure text→Markdown converters. A CSV can arrive as a pasted
string *or* an uploaded file; both go through `csv_to_markdown`, so the two paths can
never drift.

## Safety

This is the property everything else is subordinate to: **a document is never
corrupted.** Concretely —

* An extractor **never raises.** Every parser call is wrapped; a malformed, encrypted
  or corrupt file returns "nothing extracted", which the pipeline reads as "leave it
  alone". Property tests fire thousands of random byte strings at every extractor and
  assert none ever throws.
* If extraction produces nothing usable, or would produce *more* tokens than the
  base64 it replaces, the original block is forwarded **exactly** as it arrived.
* Encrypted PDFs, corrupt DOCX ZIPs, and malformed XML are all handled — the first two
  by yielding nothing, the third by lxml's recovering parser salvaging what it can.
* XML external-entity resolution is disabled, so an XXE or billion-laughs payload
  cannot read a file or exhaust memory.

## Adding a format

One extractor, one registration.

```python
# gateway/documents/extractors/pptx.py
class PptxExtractor(DocumentExtractor):
    name = "pptx"
    formats = frozenset({DocumentFormat.PPTX})

    def _extract(self, data: bytes, fmt: DocumentFormat) -> str | None:
        import pptx                       # lazy: absence degrades to "cannot extract"
        ...                               # return Markdown, or None on failure
```

```python
# gateway/documents/__init__.py :: build_document_registry
registry.register(PptxExtractor())
```

The detector already knows PPTX's signature. If a format needs a *new* signature, add
one line to `detection.py`. A heavy dependency is imported inside `_extract`, so a
deployment that does not install it simply cannot extract that format — it never
breaks startup.

## Performance and limits

Extraction runs on the request path, so two limits apply:

* **`documents_max_decoded_bytes`** (16 MB default) — a decoded document larger than
  this is forwarded unparsed. Base64 inflates ~1.33×, so this pairs with the request
  body cap.
* The whole pipeline **offloads to a worker thread** above 128 KB, so a large PDF does
  not block the event loop.

## Benchmarks

`python -m benchmarks.documents`, reproducible from a fixed seed, exact tiktoken:

| Document | Input | base64 tokens | output tokens | Reduction | Runtime | Peak mem |
|---|---|---|---|---|---|---|
| 10-page PDF | 15 KB | 14,073 | 3,923 | **72.1%** | 1.2 s | 52 MiB |
| 100-page PDF | 144 KB | 132,705 | 39,166 | **70.5%** | 11.7 s | 520 MiB |
| DOCX report | 39 KB | 35,664 | 3,087 | **91.3%** | 0.22 s | 2 MiB |
| CSV, 5k rows | 180 KB | 160,765 | 81,074 | **49.6%** | 0.02 s | 3 MiB |
| XML, 2k records | 300 KB | 269,560 | 71,113 | **73.6%** | 0.04 s | 1 MiB |

Two honest observations from these numbers:

* **PDF is memory-hungry and slow.** pdfplumber builds a Python object per character,
  so the 100-page PDF peaks at ~520 MiB and takes ~12 s. This is the phase's main
  scalability limit; see Limitations.
* **CSV reduction is modest** because a CSV's base64 is already compact ASCII. The
  extractor still helps by cleaning it and, crucially, never *grows* it — the earlier
  record-per-row form ballooned a dense table, so large tables now stay as cleaned
  CSV.

## Limitations

* **PDF memory and latency scale poorly.** A large or dense PDF can use hundreds of
  MiB and several seconds. `documents_max_decoded_bytes` bounds the worst case, but a
  page-count cap and a lighter parser are future work.
* **Scanned/image PDFs yield nothing** — there is no OCR, by design (OCR is not
  deterministic extraction). The original is forwarded.
* **PDF heading detection is heuristic** (font size relative to the body), so a PDF
  that does not vary font size produces headings-free prose. It is never *wrong*, only
  less structured.
* **PPTX, XLSX, EPUB, RTF, EML are detected but not yet extracted.**
* **Extraction changes what the provider sees.** A provider with native PDF vision
  (Claude) would no longer see the document's layout or images. Set
  `LLMGATEWAY_DOCUMENTS_ENABLED=false`, or disable a format with
  `LLMGATEWAY_DOCUMENTS_DISABLED_FORMATS=pdf`, to keep the raw document.

## Configuration

| Variable | Effect |
|---|---|
| `LLMGATEWAY_DOCUMENTS_ENABLED` | Master switch. `true` by default. |
| `LLMGATEWAY_DOCUMENTS_MAX_DECODED_BYTES` | Skip documents larger than this once decoded (16 MB). |
| `LLMGATEWAY_DOCUMENTS_DISABLED_FORMATS` | Comma-separated formats to skip, e.g. `pdf,docx`. |
