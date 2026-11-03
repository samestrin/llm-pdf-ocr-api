"""Extract a PDF page by page and render the result as Markdown or JSON.

Every page states how it was read (`text_layer` or `tesseract`). A page that
could not be read is reported as failed, with an error code and message, and
never as an empty page. Failures carry a short message only, no stack trace.
"""

import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, computed_field

from llm_pdf_ocr.ocr import DEFAULT_LANGUAGE, OcrError, TesseractNotFoundError, ocr_page
from llm_pdf_ocr.pdf import open_pdf, read_page


class Method(StrEnum):
    """How a page's text was read."""

    TEXT_LAYER = "text_layer"
    TESSERACT = "tesseract"


class ErrorCode(StrEnum):
    """Why a page failed."""

    TEXT_LAYER_FAILED = "text_layer_failed"
    OCR_FAILED = "ocr_failed"
    TESSERACT_NOT_FOUND = "tesseract_not_found"


class PageError(BaseModel):
    """Why one page failed."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str


class Page(BaseModel):
    """The result for one page. `number` is 1-based.

    A failed page has an `error` and empty `text`; an ok page has no `error`.
    `confidence` (0 to 1) is only set for OCR.
    """

    model_config = ConfigDict(extra="forbid")

    number: int
    status: Literal["ok", "failed"]
    method: Method
    text: str = ""
    confidence: float | None = None
    error: PageError | None = None


class Document(BaseModel):
    """The result for a whole PDF."""

    model_config = ConfigDict(extra="forbid")

    pages: list[Page]

    @computed_field
    @property
    def page_count(self) -> int:
        return len(self.pages)

    @computed_field
    @property
    def failed_pages(self) -> list[int]:
        return [p.number for p in self.pages if p.status == "failed"]


def extract_pdf(data: bytes, language: str = DEFAULT_LANGUAGE) -> Document:
    """Read every page: the text layer where usable, Tesseract otherwise.

    A page that fails does not stop the others. A PDF that cannot be opened
    raises a `PdfReadError`.
    """
    with open_pdf(data) as doc:
        return Document(pages=[_extract_page(page, language) for page in doc])


def render_json(doc: Document) -> str:
    """The document as JSON: `page_count`, `failed_pages` and one entry per page."""
    payload = doc.model_dump(mode="json")
    payload["pages"] = [_json_page(p) for p in payload["pages"]]
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_markdown(doc: Document) -> str:
    """One section per page, each stating its method; a failed page says so."""
    sections = []
    for page in doc.pages:
        lines = [f"## Page {page.number}", "", f"Method: {page.method}"]
        if page.confidence is not None:
            lines[-1] += f" · Confidence: {page.confidence:.2f}"
        if page.error is not None:
            lines += [
                "",
                f"> **Page {page.number} failed** ({page.error.code}): {page.error.message}",
            ]
        elif page.text:
            lines += ["", page.text]
        sections.append("\n".join(lines))
    return "\n\n".join(sections) + "\n"


def _json_page(page: dict) -> dict:
    return {
        "page": page["number"],
        "status": page["status"],
        "method": page["method"],
        "text": page["text"],
        "confidence": page["confidence"],
        "error": page["error"],
    }


def _extract_page(page, language: str) -> Page:
    number = page.number + 1
    try:
        layer = read_page(page)
    except Exception:  # one bad page must not lose the rest
        return _failed(
            number,
            Method.TEXT_LAYER,
            ErrorCode.TEXT_LAYER_FAILED,
            "the text layer could not be read",
        )
    if layer.usable:
        return Page(number=number, status="ok", method=Method.TEXT_LAYER, text=layer.text)
    try:
        result = ocr_page(page, language)
    except TesseractNotFoundError as exc:
        return _failed(number, Method.TESSERACT, ErrorCode.TESSERACT_NOT_FOUND, str(exc))
    except OcrError as exc:
        return _failed(number, Method.TESSERACT, ErrorCode.OCR_FAILED, str(exc))
    except Exception:  # e.g. a page that cannot be rendered; the message stays generic
        return _failed(
            number, Method.TESSERACT, ErrorCode.OCR_FAILED, "the page could not be recognised"
        )
    return Page(
        number=number,
        status="ok",
        method=Method.TESSERACT,
        text=result.text,
        confidence=result.confidence,
    )


def _failed(number: int, method: Method, code: ErrorCode, message: str) -> Page:
    return Page(
        number=number,
        status="failed",
        method=method,
        error=PageError(code=code, message=message or code),
    )
