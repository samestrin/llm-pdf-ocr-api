"""Read a PDF's text layer page by page with PyMuPDF.

For each page this decides whether the text layer is usable: it has text, and
that text is not just a scanner's watermark over an image or garbage from a
font with no Unicode mapping. A usable page's text is returned in reading
order; any other page is left for OCR.
"""

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

import pymupdf

# Below this share of letters and digits among non-space characters, text is garbage.
MIN_ALNUM_RATIO = 0.5
# Above this share of replacement, private-use, control or unassigned characters,
# the font had no usable Unicode mapping.
MAX_BAD_RATIO = 0.1
# A page this much covered by images, with fewer non-space characters than
# MIN_CHARS_OVER_IMAGE, is a scan with a watermark or a stray label on it.
SCAN_COVERAGE = 0.5
MIN_CHARS_OVER_IMAGE = 50
# Page images shown beside the text: this DPI, with the longer side at most this many pixels.
PAGE_IMAGE_DPI = 100
PAGE_IMAGE_MAX_SIDE = 1600

_BAD_CATEGORIES = {"Cc", "Cf", "Co", "Cn", "Cs"}
_REPLACEMENT = "\ufffd"


class PdfReadError(Exception):
    """The PDF could not be read."""


class InvalidPdfError(PdfReadError):
    """The bytes are not a PDF, or the PDF is too damaged to open."""


class EncryptedPdfError(PdfReadError):
    """The PDF needs a password to open."""


class TextLayerProblem(StrEnum):
    """Why a page's text layer is not usable."""

    NO_TEXT = "no_text"
    GARBLED_TEXT = "garbled_text"
    IMAGE_WITH_LITTLE_TEXT = "image_with_little_text"


@dataclass(frozen=True)
class PageText:
    """The text layer of one page.

    `number` is 1-based. `text` is in reading order when `usable` is true and
    is empty otherwise; `problem` says why the layer is not usable.
    """

    number: int
    usable: bool
    text: str
    problem: TextLayerProblem | None = None


@dataclass(frozen=True)
class _Line:
    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class _Block:
    lines: tuple[_Line, ...]
    bbox: tuple[float, float, float, float]


def open_pdf(data: bytes) -> pymupdf.Document:
    """Open PDF bytes, raising a PdfReadError subclass if they cannot be read."""
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (pymupdf.FileDataError, pymupdf.EmptyFileError, RuntimeError) as exc:
        raise InvalidPdfError("not a readable PDF") from exc
    if doc.needs_pass:
        doc.close()
        raise EncryptedPdfError("the PDF is encrypted")
    return doc


def read_pdf(data: bytes) -> list[PageText]:
    """Read the text layer of every page of a PDF."""
    with open_pdf(data) as doc:
        return [read_page(page) for page in doc]


def render_page_image(page: pymupdf.Page) -> bytes:
    """Render a page as a PNG at PAGE_IMAGE_DPI, its longer side capped at PAGE_IMAGE_MAX_SIDE."""
    longer = max(page.rect.width, page.rect.height)
    scale = min(PAGE_IMAGE_DPI / 72, PAGE_IMAGE_MAX_SIDE / longer)
    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")


def read_page(page: pymupdf.Page) -> PageText:
    """Read one page's text layer and decide whether it is usable."""
    text = _page_text(_reading_order(_blocks(page)))
    problem = assess_text_layer(text, _image_coverage(page))
    if problem is not None:
        return PageText(number=page.number + 1, usable=False, text="", problem=problem)
    return PageText(number=page.number + 1, usable=True, text=text)


def assess_text_layer(text: str, image_coverage: float) -> TextLayerProblem | None:
    """Return why `text` is not a usable text layer, or None if it is.

    `image_coverage` is the share of the page covered by images, 0 to 1.
    """
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return TextLayerProblem.NO_TEXT
    bad = sum(1 for c in chars if c == _REPLACEMENT or unicodedata.category(c) in _BAD_CATEGORIES)
    if bad / len(chars) > MAX_BAD_RATIO:
        return TextLayerProblem.GARBLED_TEXT
    if sum(1 for c in chars if c.isalnum()) / len(chars) < MIN_ALNUM_RATIO:
        return TextLayerProblem.GARBLED_TEXT
    if image_coverage >= SCAN_COVERAGE and len(chars) < MIN_CHARS_OVER_IMAGE:
        return TextLayerProblem.IMAGE_WITH_LITTLE_TEXT
    return None


def _image_coverage(page: pymupdf.Page) -> float:
    # Image boxes, like text boxes, are in unrotated page coordinates.
    rect = page.rect * page.derotation_matrix
    area = rect.get_area()
    if not area:
        return 0.0
    covered = sum((pymupdf.Rect(info["bbox"]) & rect).get_area() for info in page.get_image_info())
    return min(1.0, covered / area)


def _blocks(page: pymupdf.Page) -> list[_Block]:
    blocks = []
    for raw in page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)["blocks"]:
        lines = []
        for line in raw.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text:
                lines.append(_Line(text, tuple(line["bbox"])))
        if lines:
            blocks.append(_Block(tuple(lines), tuple(raw["bbox"])))
    return blocks


def _split(blocks: list[_Block], axis: int) -> list[list[_Block]]:
    """Split blocks into groups separated by empty bands along an axis (0 = x, 1 = y)."""
    groups: list[list[_Block]] = []
    end = None
    for block in sorted(blocks, key=lambda b: b.bbox[axis]):
        if end is None or block.bbox[axis] >= end:
            groups.append([block])
            end = block.bbox[axis + 2]
        else:
            groups[-1].append(block)
            end = max(end, block.bbox[axis + 2])
    return groups


def _is_columns(groups: list[list[_Block]]) -> bool:
    """Side-by-side groups are columns of prose when two of them hold multi-line blocks.

    Label/value rows are single-line blocks side by side; they read across, row by row.
    """
    return sum(1 for g in groups if any(len(b.lines) > 1 for b in g)) >= 2


def _reading_order(blocks: list[_Block]) -> list[_Block]:
    """Order blocks by recursive XY-cut: columns left to right, else bands top to bottom."""
    if len(blocks) <= 1:
        return blocks
    columns = _split(blocks, axis=0)
    rows = _split(blocks, axis=1)
    if len(columns) > 1 and (len(rows) == 1 or _is_columns(columns)):
        groups = columns
    elif len(rows) > 1:
        groups = rows
    else:
        return sorted(blocks, key=lambda b: (b.bbox[1], b.bbox[0]))
    return [block for group in groups for block in _reading_order(group)]


def _page_text(blocks: list[_Block]) -> str:
    """Join blocks as paragraphs, keeping single-line cells that share a row on one line."""
    parts: list[str] = []
    prev = None
    for block in blocks:
        if (
            prev
            and len(prev.lines) == len(block.lines) == 1
            and _same_row(prev.lines[0], block.lines[0])
        ):
            parts.append(" ")
        elif prev:
            parts.append("\n\n")
        parts.append(_block_text(block))
        prev = block
    return "".join(parts)


def _block_text(block: _Block) -> str:
    """Join a block's lines, putting lines that share a row on one line."""
    rows: list[list[_Line]] = []
    for line in sorted(block.lines, key=lambda ln: (ln.bbox[1], ln.bbox[0])):
        if rows and _same_row(rows[-1][-1], line):
            rows[-1].append(line)
        else:
            rows.append([line])
    return "\n".join(
        " ".join(ln.text for ln in sorted(row, key=lambda ln: ln.bbox[0])) for row in rows
    )


def _same_row(a: _Line, b: _Line) -> bool:
    mid = (b.bbox[1] + b.bbox[3]) / 2
    return a.bbox[1] <= mid <= a.bbox[3]
