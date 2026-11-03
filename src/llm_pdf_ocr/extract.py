"""Extract a PDF page by page and render the result as Markdown or JSON.

Every page states how it was read (`text_layer`, `tesseract` or `vision`) and, for
`vision`, which model read it. A page that could not be read is reported as failed,
with an error code and message, and never as an empty page. Failures carry a short
message only, no stack trace.

A page the vision model cannot read (a timeout, a rate limit, a bad reply) is failed
with that reason. Only when the operator sets `OCR_LLM_FALLBACK_TO_TESSERACT` is it
read by Tesseract instead, and then its `fallback` names the model and the reason.

With `method=auto` a page uses its text layer when usable, else the vision model if
one is configured, else Tesseract. A caller may force any one method instead.
"""

import json
import re
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, computed_field

from llm_pdf_ocr import vision as vision_model
from llm_pdf_ocr.config import VisionConfig
from llm_pdf_ocr.ocr import (
    DEFAULT_LANGUAGE,
    TIMEOUT_SECONDS,
    OcrError,
    OcrTimeoutError,
    TesseractNotFoundError,
    ocr_page,
)
from llm_pdf_ocr.pdf import InvalidPdfError, open_pdf, read_page


class Method(StrEnum):
    """How a page's text was read."""

    TEXT_LAYER = "text_layer"
    TESSERACT = "tesseract"
    VISION = "vision"


class MethodChoice(StrEnum):
    """How the caller asks for pages to be read: `auto`, or one method for every page."""

    AUTO = "auto"
    TEXT_LAYER = "text_layer"
    TESSERACT = "tesseract"
    VISION = "vision"


class ErrorCode(StrEnum):
    """Why a page failed."""

    TEXT_LAYER_FAILED = "text_layer_failed"
    OCR_FAILED = "ocr_failed"
    TESSERACT_NOT_FOUND = "tesseract_not_found"
    NO_TEXT_LAYER = "no_text_layer"
    VISION_FAILED = "vision_failed"
    PAGE_TIMEOUT = "page_timeout"


class VisionNotConfiguredError(ValueError):
    """The caller asked for the vision model, and none is configured."""

    code = "vision_not_configured"


class PageRangeError(ValueError):
    """A page range is malformed or names a page the PDF does not have."""

    code = "invalid_page_range"


class PageOutOfRangeError(PageRangeError):
    """A page range names a page the PDF does not have."""

    code = "page_out_of_range"


class TooManyPagesError(ValueError):
    """More pages were selected than one request may read."""

    code = "too_many_pages"


class PageError(BaseModel):
    """Why one page failed."""

    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str


class Fallback(BaseModel):
    """The method that failed before a page was read another way, and why."""

    model_config = ConfigDict(extra="forbid")

    method: Method
    model: str | None = None
    error: PageError


class Page(BaseModel):
    """The result for one page. `number` is 1-based.

    A failed page has an `error` and empty `text`; an ok page has no `error`.
    `confidence` (0 to 1) is only set for Tesseract, `model` only for `vision`.
    `fallback` is set when the vision model failed and Tesseract read the page instead.
    """

    model_config = ConfigDict(extra="forbid")

    number: int
    status: Literal["ok", "failed"]
    method: Method
    model: str | None = None
    text: str = ""
    confidence: float | None = None
    error: PageError | None = None
    fallback: Fallback | None = None


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


class Progress(Protocol):
    """Told which pages a read selects, then of each page as it is read."""

    def selected(self, numbers: Sequence[int]) -> None: ...

    def page_done(self, number: int, status: str) -> None: ...


_RANGE_ITEM = re.compile(r"(\d+)(?:-(\d+))?")


def parse_pages(spec: str, page_count: int) -> list[int]:
    """Parse a 1-based page range such as `1-3,5` into sorted, distinct page numbers.

    Raises `PageRangeError` if it is malformed and `PageOutOfRangeError` if it
    names a page beyond `page_count`.
    """
    numbers: set[int] = set()
    for item in spec.split(","):
        match = _RANGE_ITEM.fullmatch(item.strip())
        first = int(match[1]) if match else 0
        last = int(match[2] or first) if match else 0
        if first < 1 or last < first:
            raise PageRangeError(f"{spec.strip()!r} is not a page range like 1-3,5")
        if last > page_count:
            beyond = max(first, page_count + 1)
            raise PageOutOfRangeError(f"page {beyond} is not in a {page_count}-page PDF")
        numbers.update(range(first, last + 1))
    return sorted(numbers)


def select_pages(
    data: bytes,
    pages: str | None = None,
    method: MethodChoice = MethodChoice.AUTO,
    model: str | None = None,
    vision: VisionConfig | None = None,
    max_pages: int | None = None,
) -> list[int]:
    """Check a request as `extract_pdf` does before reading any page, and return
    the page numbers it would read. Raises the same errors."""
    vision = vision or VisionConfig()
    _check_method(method, model, vision)
    with open_pdf(data) as doc:
        return list(_select(doc, pages, max_pages))


def extract_pdf(
    data: bytes,
    language: str = DEFAULT_LANGUAGE,
    pages: str | None = None,
    method: MethodChoice = MethodChoice.AUTO,
    model: str | None = None,
    vision: VisionConfig | None = None,
    max_pages: int | None = None,
    page_timeout: float = TIMEOUT_SECONDS,
    progress: Progress | None = None,
) -> Document:
    """Read every page, or only those in the range `pages` (such as `1-3,5`), with
    `method`. `vision` is the operator's vision backend; without one, `auto` is the
    text layer where usable and Tesseract otherwise. Tesseract gets `page_timeout`
    seconds per page. `progress`, if given, is told the pages selected and then
    each page as it is read.

    Before any page is read, a `model` the operator does not allow raises
    `ModelNotAllowedError` (with no backend, every model is refused), forcing
    `vision` with no backend raises `VisionNotConfiguredError`, and more than
    `max_pages` selected pages raises a `TooManyPagesError`. A page that fails
    does not stop the others. A PDF that cannot be opened raises a `PdfReadError`;
    a bad range raises a `PageRangeError`.
    """
    vision = vision or VisionConfig()
    _check_method(method, model, vision)
    with open_pdf(data) as doc:
        numbers = _select(doc, pages, max_pages)
        if progress is not None:
            progress.selected(list(numbers))

        def done(page: Page) -> Page:
            if progress is not None:
                progress.page_done(page.number, page.status)
            return page

        results: dict[int, Page] = {}
        for_vision = []
        for n in numbers:
            page = _route_page(doc[n - 1], method, vision.enabled, language, page_timeout)
            if page is None:
                for_vision.append(n)
            else:
                results[n] = done(page)

        def vision_done(page: Page) -> Page:
            if page.status == "failed" and vision.fallback_to_tesseract:
                return page  # reported once Tesseract has read it
            return done(page)

        results.update(_read_with_vision(doc, for_vision, vision, model or None, vision_done))
        if vision.fallback_to_tesseract:
            for n in for_vision:
                if results[n].status == "failed":
                    results[n] = done(_fall_back(doc[n - 1], results[n], language, page_timeout))
        return Document(pages=[results[n] for n in numbers])


def _check_method(method: MethodChoice, model: str | None, vision: VisionConfig) -> None:
    if model:
        vision.resolve_model(model)
    if method is MethodChoice.VISION and not vision.enabled:
        raise VisionNotConfiguredError("the vision backend is not configured")


def _select(doc, pages: str | None, max_pages: int | None) -> Sequence[int]:
    if not len(doc):
        raise InvalidPdfError("the PDF has no pages")
    numbers = range(1, len(doc) + 1) if pages is None else parse_pages(pages, len(doc))
    if max_pages is not None and len(numbers) > max_pages:
        raise TooManyPagesError(
            f"{len(numbers)} pages selected; the limit is {max_pages}. Ask for fewer with `pages`"
        )
    return numbers


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
        if page.model is not None:
            lines[-1] += f" · Model: {page.model}"
        if page.confidence is not None:
            lines[-1] += f" · Confidence: {page.confidence:.2f}"
        if page.fallback is not None:
            failed = page.fallback
            lines += [
                "",
                f"> Read with Tesseract because {failed.method} ({failed.model}) failed "
                f"({failed.error.code}): {failed.error.message}",
            ]
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
        "model": page["model"],
        "text": page["text"],
        "confidence": page["confidence"],
        "error": page["error"],
        "fallback": page["fallback"],
    }


def _route_page(
    page, method: MethodChoice, vision_enabled: bool, language: str, timeout: float
) -> Page | None:
    """Read one page by any method but vision; None means it goes to the vision model."""
    if method is MethodChoice.VISION:
        return None
    if method is MethodChoice.TESSERACT:
        return _ocr(page, language, timeout)
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
    if method is MethodChoice.TEXT_LAYER:
        return _failed(
            number, Method.TEXT_LAYER, ErrorCode.NO_TEXT_LAYER, "the page has no usable text layer"
        )
    if vision_enabled:
        return None
    return _ocr(page, language, timeout)


def _ocr(page, language: str, timeout: float) -> Page:
    number = page.number + 1
    try:
        result = ocr_page(page, language, timeout=timeout)
    except TesseractNotFoundError as exc:
        return _failed(number, Method.TESSERACT, ErrorCode.TESSERACT_NOT_FOUND, str(exc))
    except OcrTimeoutError as exc:
        return _failed(number, Method.TESSERACT, ErrorCode.PAGE_TIMEOUT, str(exc))
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


def _read_with_vision(
    doc,
    numbers: list[int],
    config: VisionConfig,
    model: str | None,
    done: Callable[[Page], Page],
) -> dict[int, Page]:
    """Read pages with the vision model, at most `config.concurrency` at once."""
    if not numbers:
        return {}
    resolved = config.resolve_model(model)
    results: dict[int, Page] = {}
    images = []
    # Rendering stays on this thread: PyMuPDF documents are not thread-safe.
    for n in numbers:
        try:
            images.append((n, vision_model.render_page(doc[n - 1])))
        except Exception:  # one bad page must not lose the rest
            results[n] = done(
                _failed(
                    n,
                    Method.VISION,
                    ErrorCode.VISION_FAILED,
                    "the page could not be rendered",
                    model=resolved,
                )
            )

    def read(item: tuple[int, bytes]) -> Page:
        n, png = item
        try:
            result = vision_model.read_image(png, n, config, model)
        except vision_model.VisionError as exc:
            return _failed(n, Method.VISION, ErrorCode.VISION_FAILED, str(exc), model=resolved)
        except Exception:  # the message stays generic
            return _failed(
                n,
                Method.VISION,
                ErrorCode.VISION_FAILED,
                "the vision model could not read the page",
                model=resolved,
            )
        return Page(
            number=n, status="ok", method=Method.VISION, model=result.model, text=result.text
        )

    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        for future in as_completed([pool.submit(read, item) for item in images]):
            page = done(future.result())
            results[page.number] = page
    return results


def _fall_back(page, failed: Page, language: str, timeout: float) -> Page:
    """Read a page the vision model failed on with Tesseract, recording why."""
    fallback = Fallback(method=failed.method, model=failed.model, error=failed.error)
    return _ocr(page, language, timeout).model_copy(update={"fallback": fallback})


def _failed(
    number: int, method: Method, code: ErrorCode, message: str, model: str | None = None
) -> Page:
    return Page(
        number=number,
        status="failed",
        method=method,
        model=model,
        error=PageError(code=code, message=message or code),
    )
