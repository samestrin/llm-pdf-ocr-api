"""Rebuild the benchmark PDFs, reference text and manifest.json.

Run from the repo root: `uv run python benchmarks/build.py [DOWNLOAD_DIR]`.
The outputs are committed, so tests never depend on this script or the network.

Every entry is cut from a public source PDF (a few pages each). Reference text
is the page text of the source, read in reading order, with table regions left
out; tables live in `reference/<id>.tables.md` as Markdown, written by hand
from the rendered page. Scanned entries are the same pages rasterised into
image-only PDFs, so their reference is exact. "Poor" scans are rasterised at
low resolution, skewed and heavily JPEG-compressed.
"""

import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
PDFS = HERE / "pdfs"
REF = HERE / "reference"

PD_US = "Public domain (US federal government work, 17 U.S.C. 105)"
BY = "CC BY 4.0"


def arxiv(aid: str, title: str, authors: str) -> dict:
    return {
        "url": f"https://arxiv.org/pdf/{aid}",
        "license": BY,
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "attribution": f"{authors}: {title} (arXiv:{aid}), {BY}",
    }


SOURCES = {
    "constitution": {
        "url": "https://www.govinfo.gov/content/pkg/CDOC-110hdoc50/pdf/CDOC-110hdoc50.pdf",
        "license": PD_US,
        "license_url": "https://www.law.cornell.edu/uscode/text/17/105",
        "attribution": "US House of Representatives, Document 110-50: The Constitution",
    },
    "nistcsf": {
        "url": "https://nvlpubs.nist.gov/nistpubs/CSWP/NIST.CSWP.29.pdf",
        "license": PD_US,
        "license_url": "https://www.law.cornell.edu/uscode/text/17/105",
        "attribution": "NIST CSWP 29: The NIST Cybersecurity Framework (CSF) 2.0",
    },
    "sp80063": {
        "url": "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-63-3.pdf",
        "license": PD_US,
        "license_url": "https://www.law.cornell.edu/uscode/text/17/105",
        "attribution": "NIST SP 800-63-3: Digital Identity Guidelines",
    },
    "fw9": {
        "url": "https://www.irs.gov/pub/irs-pdf/fw9.pdf",
        "license": PD_US,
        "license_url": "https://www.law.cornell.edu/uscode/text/17/105",
        "attribution": "IRS Form W-9",
    },
    "f1040": {
        "url": "https://www.irs.gov/pub/irs-pdf/f1040.pdf",
        "license": PD_US,
        "license_url": "https://www.law.cornell.edu/uscode/text/17/105",
        "attribution": "IRS Form 1040",
    },
    "fw4": {
        "url": "https://www.irs.gov/pub/irs-pdf/fw4.pdf",
        "license": PD_US,
        "license_url": "https://www.law.cornell.edu/uscode/text/17/105",
        "attribution": "IRS Form W-4",
    },
    "llama": arxiv(
        "2302.13971", "LLaMA: Open and Efficient Foundation Language Models", "Touvron et al."
    ),
    "vllm": arxiv(
        "2309.06180",
        "Efficient Memory Management for Large Language Model Serving with PagedAttention",
        "Kwon et al.",
    ),
    "llava": arxiv("2310.03744", "Improved Baselines with Visual Instruction Tuning", "Liu et al."),
    "cot": arxiv(
        "2201.11903",
        "Chain-of-Thought Prompting Elicits Reasoning in Large Language Models",
        "Wei et al.",
    ),
    "mixtral": arxiv("2401.04088", "Mixtral of Experts", "Jiang et al."),
    "mistral": arxiv("2310.06825", "Mistral 7B", "Jiang et al."),
}

MIXTRAL_P5 = [[100, 50, 510, 215], [100, 370, 510, 420]]

# kind: born-digital | two-column | tables | form | scanned | poor-scan
# `exclude` rectangles hold table blocks; they stay out of the reference text.
ENTRIES = [
    ("us-constitution-text", "constitution", [11], "born-digital", {}),
    ("nist-csf-2-overview", "nistcsf", [2], "born-digital", {}),
    ("nist-sp800-63-3-text", "sp80063", [8], "born-digital", {}),
    ("cot-paper-first-page", "cot", [1], "born-digital", {}),
    ("llama-two-column", "llama", [1, 2], "two-column", {}),
    ("vllm-two-column", "vllm", [1, 2], "two-column", {}),
    ("llava-two-column", "llava", [1, 2], "two-column", {}),
    ("mixtral-tables-p4", "mixtral", [4], "tables", {"exclude": [[100, 70, 510, 170]]}),
    ("mixtral-tables-p5", "mixtral", [5], "tables", {"exclude": MIXTRAL_P5}),
    ("mistral-guardrails-p5", "mistral", [5], "tables", {"exclude": [[350, 515, 510, 570]]}),
    ("irs-w9-form", "fw9", [1], "form", {}),
    ("irs-1040-form", "f1040", [1], "form", {}),
    ("irs-w4-form", "fw4", [1], "form", {}),
    ("us-constitution-scan", "constitution", [12], "scanned", {}),
    ("nist-csf-2-scan", "nistcsf", [4], "scanned", {}),
    ("llama-first-page-scan", "llama", [1], "scanned", {}),
    ("sp800-63-3-poor-scan", "sp80063", [9], "poor-scan", {}),
    ("irs-w9-poor-scan", "fw9", [1], "poor-scan", {}),
    ("cot-poor-scan", "cot", [1], "poor-scan", {}),
    (
        "mixtral-tables-p5-poor-scan",
        "mixtral",
        [5],
        "poor-scan",
        {"tables_from": "mixtral-tables-p5", "exclude": MIXTRAL_P5},
    ),
]


def fetch(key: str, cache: Path) -> Path:
    path = cache / f"{key}.pdf"
    if not path.exists():
        req = urllib.request.Request(SOURCES[key]["url"], headers={"User-Agent": "llm-pdf-ocr-api"})
        path.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    return path


LIGATURES = str.maketrans({"ﬁ": "fi", "ﬂ": "fl", "ﬀ": "ff", "ﬃ": "ffi", "ﬄ": "ffl"})


def page_text(page: pymupdf.Page, exclude: list[list[float]], sort: bool) -> str:
    blocks = page.get_text("blocks", sort=sort)
    kept = []
    for x0, y0, x1, y1, text, _, kind in blocks:
        if kind != 0:
            continue
        if any(x0 >= r[0] and y0 >= r[1] and x1 <= r[2] and y1 <= r[3] for r in exclude):
            continue
        kept.append(text.strip())
    return re.sub(r"[ \t]+\n", "\n", "\n".join(kept)).translate(LIGATURES)


def cut(src: pymupdf.Document, pages: list[int]) -> pymupdf.Document:
    out = pymupdf.open()
    for n in pages:
        out.insert_pdf(src, from_page=n - 1, to_page=n - 1)
    return out


def rasterise(src: pymupdf.Document, pages: list[int], poor: bool) -> pymupdf.Document:
    out = pymupdf.open()
    for n in pages:
        page = src[n - 1]
        if poor:
            matrix = pymupdf.Matrix(1, 1).prerotate(1.8)
            pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csGRAY)
            data = pix.tobytes("jpeg", jpg_quality=12)
        else:
            data = page.get_pixmap(dpi=150).tobytes("jpeg", jpg_quality=85)
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=data, keep_proportion=False)
    return out


def main() -> None:
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    PDFS.mkdir(exist_ok=True)
    REF.mkdir(exist_ok=True)
    text_of: dict[str, str] = {}
    manifest = []
    for eid, key, pages, kind, opts in ENTRIES:
        src = pymupdf.open(fetch(key, cache))
        text = "\n\n".join(
            page_text(src[n - 1], opts.get("exclude", []), sort=kind == "form") for n in pages
        )
        text_of[eid] = text
        if kind in ("scanned", "poor-scan"):
            doc = rasterise(src, pages, poor=kind == "poor-scan")
        else:
            doc = cut(src, pages)
        doc.save(PDFS / f"{eid}.pdf", garbage=4, deflate=True)
        (REF / f"{eid}.txt").write_text(text + "\n", encoding="utf-8")
        entry = {
            "id": eid,
            "pdf": f"pdfs/{eid}.pdf",
            "kind": kind,
            "source_url": SOURCES[key]["url"],
            "source_pages": pages,
            "license": SOURCES[key]["license"],
            "license_url": SOURCES[key]["license_url"],
            "attribution": SOURCES[key]["attribution"],
            "reference_text": f"reference/{eid}.txt",
        }
        tables = REF / f"{opts.get('tables_from', eid)}.tables.md"
        if tables.exists():
            if "tables_from" in opts:
                shutil.copy(tables, REF / f"{eid}.tables.md")
            entry["reference_tables"] = f"reference/{eid}.tables.md"
        if kind in ("scanned", "poor-scan"):
            entry["derived"] = (
                "page(s) rasterised from the source into an image-only PDF; no text layer"
            )
        manifest.append(entry)
    (HERE / "manifest.json").write_text(
        json.dumps({"version": 1, "entries": manifest}, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
