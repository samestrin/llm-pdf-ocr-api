# Benchmark set

Twenty PDFs, each a few pages cut from a public-domain or freely licensed source, with the text a reader should recover. `manifest.json` lists every file with its source URL, license, attribution and reference files; `tests/test_benchmarks.py` fails CI if a PDF has no entry, or an entry has no source, license or reference text.

| Kind | Files | What it exercises |
|---|---|---|
| `born-digital` | 4 | Clean text layer, one column |
| `two-column` | 3 | Reading order across columns (arXiv papers) |
| `tables` | 3 | Tables, reference stored as Markdown |
| `form` | 3 | IRS forms: boxes, labels, small print |
| `scanned` | 3 | Image-only pages, no text layer |
| `poor-scan` | 4 | 72 dpi, skewed, heavily compressed; one has tables |

## Files

- `pdfs/<id>.pdf`: the input.
- `reference/<id>.txt`: reference text in reading order. Table regions are left out of it.
- `reference/<id>.tables.md`: the tables on the page as Markdown tables (`tables` entries and the poor scan of one).
- `manifest.json`: one entry per PDF (`source_url`, `license`, `license_url`, `attribution`, `kind`, `source_pages`).

## How the references were made

Reference text is the source PDF's own text layer for the same pages, read in reading order, with ligatures expanded and table regions removed. It is not hand-proofread line by line, so it can carry the source's own quirks (hyphenation at line ends, form field labels in layout order). Reference tables were typed by hand from the rendered page. Scanned entries are the same pages rasterised into image-only PDFs, so their reference text is exact for the content.

## Licenses

US federal government works (the Constitution document, NIST publications, IRS forms) are in the public domain. The arXiv papers are CC BY 4.0, and the manifest carries the attribution. Pages are excerpts; nothing is altered except the rasterised scans.

## Running

    uv run python benchmarks/run.py

reads every entry with each method available: the text layer, Tesseract (when it is installed), and the vision model only when `OCR_LLM_BASE_URL` is set, with the same `OCR_LLM_*` settings as the service (see [docs/vision-backend.md](../docs/vision-backend.md)). It prints the table and writes it to `results/<date>.md`, with the same numbers in `results/<date>.json`. `--only id,id` runs a subset, `--methods text_layer,tesseract` picks methods, `--out` and `--date` change where it writes.

| Column | How it is measured |
|---|---|
| CER | Character error rate: edit distance to `reference/<id>.txt` over the reference's length, after collapsing whitespace, capped at 1. Markdown tables, heading marks and bold marks are left out of the output first, as the reference leaves tables out. |
| Text score | 1 − CER. |
| Table F1 | Only for entries with `reference/<id>.tables.md`: the cells of the Markdown tables in the output against the reference cells, as a multiset of cell texts (case and whitespace ignored, empty cells skipped). Output with no Markdown table recovers no cells, so plain-text methods score 0 here. |
| Seconds per page | Wall time to read the document over its pages. |
| Failed | Pages the method could not read, such as the text layer on a scan (`no_text_layer`). |

A document with a failed page scores 0 (CER 1, table F1 0) and stays in every average; it is never skipped. The reference text is per document, not per page, so one failed page fails its document's score. The vision model is measured on its own: `OCR_LLM_FALLBACK_TO_TESSERACT` is ignored here, so a page the model fails counts as failed.

The results name the vision model (`OCR_LLM_DEFAULT_MODEL`) and never the endpoint URL, its host or the API key; the runner refuses to write a file that would contain them.

## Rebuilding

`uv run python benchmarks/build.py [DOWNLOAD_DIR]` downloads the sources and regenerates `pdfs/`, `reference/*.txt` and `manifest.json`. The tables files are hand-written and are not regenerated. The outputs are committed and the tests never use the network.
