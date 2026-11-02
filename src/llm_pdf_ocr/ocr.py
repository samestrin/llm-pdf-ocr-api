"""OCR pages that have no usable text layer, with Tesseract.

A page is rendered at a fixed DPI, handed to the `tesseract` command, and the
words it reports are put back into lines and paragraphs. The confidence is the
mean of Tesseract's per-word confidences, scaled to 0 to 1.
"""

import subprocess
from dataclasses import dataclass

import pymupdf

from llm_pdf_ocr.pdf import open_pdf

# Tesseract is tuned for about 300 DPI input; fixed so results do not vary with page size.
OCR_DPI = 300
DEFAULT_LANGUAGE = "eng"
TIMEOUT_SECONDS = 120

_WORD_LEVEL = 5
_TSV_COLUMNS = 12


class OcrError(Exception):
    """Tesseract could not recognise a page."""


class TesseractNotFoundError(OcrError):
    """The `tesseract` command is not installed."""


@dataclass(frozen=True)
class OcrResult:
    """The recognised text of one page.

    `number` is 1-based. `confidence` is 0 to 1; it is 0 when no text was found.
    """

    number: int
    text: str
    confidence: float


def ocr_page(page: pymupdf.Page, language: str = DEFAULT_LANGUAGE) -> OcrResult:
    """Render a page at OCR_DPI and recognise its text."""
    png = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY).tobytes("png")
    text, confidence = _parse_tsv(_run_tesseract(png, language))
    return OcrResult(number=page.number + 1, text=text, confidence=confidence)


def ocr_pdf_pages(
    data: bytes, numbers: list[int], language: str = DEFAULT_LANGUAGE
) -> list[OcrResult]:
    """Recognise the given 1-based pages of a PDF, in the order given."""
    with open_pdf(data) as doc:
        for number in numbers:
            if not 1 <= number <= len(doc):
                raise ValueError(f"page {number} is not in a {len(doc)}-page PDF")
        return [ocr_page(doc[number - 1], language) for number in numbers]


def _run_tesseract(png: bytes, language: str) -> str:
    cmd = ["tesseract", "stdin", "stdout", "-l", language, "tsv"]
    try:
        proc = subprocess.run(cmd, input=png, capture_output=True, timeout=TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        raise TesseractNotFoundError("tesseract is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise OcrError(f"tesseract timed out after {TIMEOUT_SECONDS} seconds") from exc
    if proc.returncode != 0:
        raise OcrError(proc.stderr.decode("utf-8", "replace").strip() or "tesseract failed")
    return proc.stdout.decode("utf-8", "replace")


def _parse_tsv(tsv: str) -> tuple[str, float]:
    """Rebuild text from Tesseract's TSV: lines of words, paragraphs split by a blank line."""
    paragraphs: dict[tuple[str, str], dict[str, list[str]]] = {}
    confidences: list[float] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t", _TSV_COLUMNS - 1)
        if len(cols) < _TSV_COLUMNS or cols[0] != str(_WORD_LEVEL):
            continue
        word, conf = cols[11].strip(), float(cols[10])
        if not word or conf < 0:
            continue
        lines = paragraphs.setdefault((cols[2], cols[3]), {})
        lines.setdefault(cols[4], []).append(word)
        confidences.append(conf)
    text = "\n\n".join(
        "\n".join(" ".join(words) for words in lines.values()) for lines in paragraphs.values()
    )
    confidence = sum(confidences) / len(confidences) / 100 if confidences else 0.0
    return text, confidence
