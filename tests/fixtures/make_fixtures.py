"""Regenerate the PDF fixtures in this folder.

Run from the repo root: `uv run python tests/fixtures/make_fixtures.py`.
The PDFs are committed, so tests never depend on this script running.

- born_digital.pdf: every page has a real text layer.
  Page 1 is a full-width title over two columns; page 2 is one column
  followed by label/value rows. Both pages write their content out of
  reading order, so a reader that follows the content stream gets it wrong.
- scanned.pdf: no usable text layer. Page 1 is only an image; page 2 is an
  image with a scanner's watermark text and nothing else.
- mixed.pdf: page 1 born-digital, page 2 image only, page 3 an image with a
  garbled text layer, page 4 a searchable scan (image plus invisible OCR text).
"""

from pathlib import Path

import pymupdf

HERE = Path(__file__).parent
A4 = pymupdf.paper_rect("a4")
MARGIN = 56
GUTTER = 24

TITLE = "Quarterly Report"
LEFT = [
    "Left column first paragraph. Revenue grew in every region this quarter, "
    "led by strong demand for the new product line.",
    "Left column second paragraph. Costs stayed flat because the team "
    "renegotiated its largest supplier contracts early.",
]
RIGHT = [
    "Right column first paragraph. Hiring slowed as planned, and the open "
    "roles that remain are mostly in support.",
    "Right column second paragraph. The outlook for next quarter is steady, "
    "with no large launches on the calendar.",
]
SINGLE = (
    "Single column paragraph. This page has one column of text that runs the "
    "full width of the page, followed by a few label and value rows."
)
ROWS = [("Invoice", "INV-0042"), ("Date", "2024-03-01"), ("Total", "1,250.00")]
SCAN_TEXT = "Scanned page text. This line exists only as pixels in an image."
OCR_TEXT = (
    "Searchable scan text. An OCR tool put this invisible text over the image, "
    "so the text layer is real even though the page is a picture."
)
WATERMARK = "Scanned by CamScanner"
GARBLE = "~ ' . , ^ ; : _ | . ' ` ~ - , . ; ' ^ ~ ` | : , . _ ' ~"


def _textbox(page: pymupdf.Page, rect: pymupdf.Rect, text: str, **kw) -> None:
    left = page.insert_textbox(rect, text, fontname="helv", fontsize=11, **kw)
    if left < 0:
        raise ValueError(f"text does not fit in {rect}: {text[:30]!r}")


def two_column_page(doc: pymupdf.Document) -> None:
    """Content is written out of reading order: right column first, title last."""
    page = doc.new_page(width=A4.width, height=A4.height)
    mid = A4.width / 2
    for i in range(len(LEFT)):
        y = MARGIN + 60 + i * 100
        _textbox(page, pymupdf.Rect(mid + GUTTER / 2, y, A4.width - MARGIN, y + 80), RIGHT[i])
        _textbox(page, pymupdf.Rect(MARGIN, y, mid - GUTTER / 2, y + 80), LEFT[i])
    _textbox(page, pymupdf.Rect(MARGIN, MARGIN, A4.width - MARGIN, MARGIN + 30), TITLE)


def single_column_page(doc: pymupdf.Document) -> None:
    page = doc.new_page(width=A4.width, height=A4.height)
    _textbox(page, pymupdf.Rect(MARGIN, MARGIN, A4.width - MARGIN, MARGIN + 60), SINGLE)
    # Values are written before their labels, so content order is not reading order.
    for i, (_, value) in enumerate(ROWS):
        page.insert_text((MARGIN + 200, MARGIN + 100 + i * 20), value, fontname="helv", fontsize=11)
    for i, (label, _) in enumerate(ROWS):
        page.insert_text((MARGIN, MARGIN + 100 + i * 20), label, fontname="helv", fontsize=11)


def _scan_image(text: str) -> bytes:
    """Render `text` on a blank page and return it as a grayscale PNG."""
    src = pymupdf.open()
    page = src.new_page(width=A4.width, height=A4.height)
    _textbox(page, pymupdf.Rect(MARGIN, MARGIN, A4.width - MARGIN, MARGIN + 80), text)
    return page.get_pixmap(dpi=100, colorspace=pymupdf.csGRAY).tobytes("png")


def scanned_page(doc: pymupdf.Document, text: str = SCAN_TEXT) -> pymupdf.Page:
    page = doc.new_page(width=A4.width, height=A4.height)
    page.insert_image(page.rect, stream=_scan_image(text))
    return page


def watermarked_scan_page(doc: pymupdf.Document) -> None:
    page = scanned_page(doc)
    page.insert_text((MARGIN, A4.height - 20), WATERMARK, fontname="helv", fontsize=8)


def garbled_scan_page(doc: pymupdf.Document) -> None:
    page = scanned_page(doc)
    _textbox(page, pymupdf.Rect(MARGIN, MARGIN, A4.width - MARGIN, MARGIN + 80), GARBLE)


def searchable_scan_page(doc: pymupdf.Document) -> None:
    page = scanned_page(doc, OCR_TEXT)
    rect = pymupdf.Rect(MARGIN, MARGIN, A4.width - MARGIN, MARGIN + 80)
    _textbox(page, rect, OCR_TEXT, render_mode=3)


def save(doc: pymupdf.Document, name: str) -> None:
    doc.set_metadata({"producer": "llm-pdf-ocr-api test fixtures", "creator": ""})
    doc.save(HERE / name, garbage=4, deflate=True, no_new_id=True)


def main() -> None:
    doc = pymupdf.open()
    two_column_page(doc)
    single_column_page(doc)
    save(doc, "born_digital.pdf")

    doc = pymupdf.open()
    scanned_page(doc)
    watermarked_scan_page(doc)
    save(doc, "scanned.pdf")

    doc = pymupdf.open()
    single_column_page(doc)
    scanned_page(doc)
    garbled_scan_page(doc)
    searchable_scan_page(doc)
    save(doc, "mixed.pdf")


if __name__ == "__main__":
    main()
