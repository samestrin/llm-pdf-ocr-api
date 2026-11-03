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

## Rebuilding

`uv run python benchmarks/build.py [DOWNLOAD_DIR]` downloads the sources and regenerates `pdfs/`, `reference/*.txt` and `manifest.json`. The tables files are hand-written and are not regenerated. The outputs are committed and the tests never use the network.
