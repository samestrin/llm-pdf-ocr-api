import shutil
import subprocess
from difflib import SequenceMatcher
from pathlib import Path

import pymupdf
import pytest

from llm_pdf_ocr import ocr
from llm_pdf_ocr.ocr import OcrError, OcrResult, TesseractNotFoundError, ocr_page, ocr_pdf_pages

FIXTURES = Path(__file__).parent / "fixtures"
SCAN_TEXT = "Scanned page text. This line exists only as pixels in an image."
MIN_SIMILARITY = 0.9

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is not installed"
)

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"  # noqa: E501


def tsv_row(level, block, par, line, word, conf, text):
    return f"{level}\t1\t{block}\t{par}\t{line}\t{word}\t0\t0\t10\t10\t{conf}\t{text}\n"


def fake_tesseract(monkeypatch, stdout="", returncode=0, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout.encode(), stderr.encode())

    monkeypatch.setattr(ocr.subprocess, "run", run)
    return calls


def blank_page() -> pymupdf.Page:
    doc = pymupdf.open()
    return doc.new_page(width=72, height=36)


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, " ".join(a.split()).lower(), " ".join(b.split()).lower()).ratio()


# Real Tesseract on the scanned fixture


@needs_tesseract
def test_scanned_fixture_returns_its_known_text():
    result = ocr_pdf_pages((FIXTURES / "scanned.pdf").read_bytes(), [1])[0]

    assert result.number == 1
    assert similarity(result.text, SCAN_TEXT) >= MIN_SIMILARITY
    assert 0.5 <= result.confidence <= 1.0


@needs_tesseract
def test_blank_page_returns_empty_text_with_zero_confidence():
    result = ocr_page(blank_page())

    assert result == OcrResult(number=1, text="", confidence=0.0)


# Rendering and the Tesseract call


def test_page_is_rendered_at_the_fixed_dpi(monkeypatch):
    calls = fake_tesseract(monkeypatch, HEADER)
    page = blank_page()

    ocr_page(page)

    png = calls[0][1]["input"]
    pixmap = pymupdf.Pixmap(png)
    assert ocr.OCR_DPI == 300
    assert (pixmap.width, pixmap.height) == (300, 150)  # 72x36 pt at 300 dpi


def test_tesseract_is_called_for_tsv_output_in_the_configured_language(monkeypatch):
    calls = fake_tesseract(monkeypatch, HEADER)

    ocr_page(blank_page(), language="deu")

    assert calls[0][0] == ["tesseract", "stdin", "stdout", "-l", "deu", "tsv"]


# Parsing


def test_words_lines_and_paragraphs_are_rebuilt_from_tsv(monkeypatch):
    fake_tesseract(
        monkeypatch,
        HEADER
        + tsv_row(4, 1, 1, 1, 0, -1, "")
        + tsv_row(5, 1, 1, 1, 1, 96.5, "Hello")
        + tsv_row(5, 1, 1, 1, 2, 90.5, "world")
        + tsv_row(5, 1, 1, 2, 1, 93, "again")
        + tsv_row(5, 1, 2, 1, 1, 100, "Next")
        + tsv_row(5, 2, 1, 1, 1, 80, "Block"),
    )

    result = ocr_page(blank_page())

    assert result.text == "Hello world\nagain\n\nNext\n\nBlock"
    assert result.confidence == pytest.approx(0.92)


def test_blank_and_unrecognised_words_are_ignored(monkeypatch):
    fake_tesseract(
        monkeypatch,
        HEADER
        + tsv_row(5, 1, 1, 1, 1, 90, "kept")
        + tsv_row(5, 1, 1, 1, 2, -1, "dropped")
        + tsv_row(5, 1, 1, 1, 3, 95, "   "),
    )

    result = ocr_page(blank_page())

    assert result.text == "kept"
    assert result.confidence == pytest.approx(0.9)


def test_short_rows_and_non_word_rows_are_skipped(monkeypatch):
    fake_tesseract(
        monkeypatch,
        HEADER
        + "5\t1\n"
        + tsv_row(4, 1, 1, 1, 0, 88, "line level row with a real confidence")
        + tsv_row(5, 1, 1, 1, 1, 90, "kept"),
    )

    result = ocr_page(blank_page())

    assert result.text == "kept"
    assert result.confidence == pytest.approx(0.9)


def test_page_number_is_one_based(monkeypatch):
    fake_tesseract(monkeypatch, HEADER)
    doc = pymupdf.open()
    doc.new_page()
    page = doc.new_page()

    assert ocr_page(page).number == 2


# Errors


def test_missing_tesseract_is_reported(monkeypatch):
    def run(cmd, **kwargs):
        raise FileNotFoundError("tesseract")

    monkeypatch.setattr(ocr.subprocess, "run", run)

    with pytest.raises(TesseractNotFoundError):
        ocr_page(blank_page())


def test_tesseract_failure_carries_its_message(monkeypatch):
    fake_tesseract(monkeypatch, returncode=1, stderr="Failed loading language 'xx'")

    with pytest.raises(OcrError, match="Failed loading language"):
        ocr_page(blank_page(), language="xx")


def test_tesseract_failure_without_a_message_still_says_it_failed(monkeypatch):
    fake_tesseract(monkeypatch, returncode=1, stderr="")

    with pytest.raises(OcrError, match="tesseract failed"):
        ocr_page(blank_page())


def test_tesseract_timeout_is_an_ocr_error(monkeypatch):
    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(ocr.subprocess, "run", run)

    with pytest.raises(OcrError, match="timed out"):
        ocr_page(blank_page())


def test_ocr_errors_share_a_base_class():
    assert issubclass(TesseractNotFoundError, OcrError)


# Selecting pages


def test_only_the_requested_pages_are_recognised(monkeypatch):
    calls = fake_tesseract(monkeypatch, HEADER + tsv_row(5, 1, 1, 1, 1, 90, "x"))

    results = ocr_pdf_pages((FIXTURES / "mixed.pdf").read_bytes(), [2, 3])

    assert [r.number for r in results] == [2, 3]
    assert len(calls) == 2


def test_a_page_number_outside_the_document_is_rejected(monkeypatch):
    fake_tesseract(monkeypatch, HEADER)

    with pytest.raises(ValueError, match="page 9"):
        ocr_pdf_pages((FIXTURES / "scanned.pdf").read_bytes(), [9])


def test_unreadable_pdf_raises_the_pdf_error():
    from llm_pdf_ocr.pdf import InvalidPdfError

    with pytest.raises(InvalidPdfError):
        ocr_pdf_pages(b"nope", [1])
