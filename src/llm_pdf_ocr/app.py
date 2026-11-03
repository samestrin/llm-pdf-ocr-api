"""FastAPI application for llm-pdf-ocr-api.

A PDF that cannot be read, or a bad page range, is answered with 400 and
`{"error": {"code": ..., "message": ...}}`, never a stack trace. So is a `model`
outside the operator's list, and `method=vision` with no vision backend. A page
that fails is reported in the result, and the other pages are still returned.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from functools import cache
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from llm_pdf_ocr import __version__
from llm_pdf_ocr.config import ModelNotAllowedError, VisionConfig, load_vision_config
from llm_pdf_ocr.extract import (
    MethodChoice,
    PageRangeError,
    VisionNotConfiguredError,
    extract_pdf,
    render_json,
    render_markdown,
)
from llm_pdf_ocr.pdf import EncryptedPdfError, PdfReadError


@cache
def get_vision_config() -> VisionConfig:
    """The vision backend settings, read from the environment once."""
    return load_vision_config()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_vision_config()  # a bad OCR_LLM_* value stops the service at start, not per request
    yield


app = FastAPI(
    lifespan=_lifespan,
    title="llm-pdf-ocr-api",
    version=__version__,
    description="Turn a PDF, scanned or not, into Markdown or JSON, page by page.",
)


class Format(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"


def _error(code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": {"code": code, "message": message}})


@app.exception_handler(PdfReadError)
def _pdf_read_error(request: Request, exc: PdfReadError) -> JSONResponse:
    code = "encrypted_pdf" if isinstance(exc, EncryptedPdfError) else "invalid_pdf"
    return _error(code, str(exc))


@app.exception_handler(PageRangeError)
def _page_range_error(request: Request, exc: PageRangeError) -> JSONResponse:
    return _error(exc.code, str(exc))


@app.exception_handler(ModelNotAllowedError)
def _model_not_allowed(request: Request, exc: ModelNotAllowedError) -> JSONResponse:
    return _error("model_not_allowed", str(exc))


@app.exception_handler(VisionNotConfiguredError)
def _vision_not_configured(request: Request, exc: VisionNotConfiguredError) -> JSONResponse:
    return _error(exc.code, str(exc))


Vision = Annotated[VisionConfig, Depends(get_vision_config)]


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the service is up."""
    return {"status": "ok"}


@app.post(
    "/v1/extract",
    response_class=Response,
    responses={
        200: {
            "description": "One section or entry per page, each naming how it was read.",
            "content": {"text/markdown": {}, "application/json": {}},
        },
        400: {
            "description": "The PDF cannot be read, the page range is bad, the model is not "
            "allowed, or `vision` was asked for and no vision model is configured."
        },
    },
)
def extract(
    file: Annotated[UploadFile, File(description="The PDF.")],
    format: Annotated[Format, Form(description="`markdown` or `json`.")] = Format.MARKDOWN,
    pages: Annotated[
        str | None, Form(description="1-based pages to read, such as `1-3,5`. Default: all.")
    ] = None,
    method: Annotated[
        MethodChoice,
        Form(
            description="`auto` (default): the text layer when usable, else the vision model "
            "if configured, else Tesseract. Or `text_layer`, `tesseract` or `vision` for "
            "every page."
        ),
    ] = MethodChoice.AUTO,
    model: Annotated[
        str | None,
        Form(description="Vision model, from the operator's list. Default: the operator's."),
    ] = None,
    *,
    vision: Vision,
) -> Response:
    """Extract a PDF's text page by page: from the text layer, a vision model or Tesseract."""
    doc = extract_pdf(file.file.read(), pages=pages, method=method, model=model, vision=vision)
    if format is Format.JSON:
        return Response(render_json(doc), media_type="application/json")
    return Response(render_markdown(doc), media_type="text/markdown; charset=utf-8")


@app.post("/ocr", deprecated=True)
def ocr(
    file: Annotated[UploadFile, File(description="The PDF.")], response: Response, vision: Vision
) -> dict:
    """Deprecated alias kept for 0.0.1: the whole PDF's text as `{"text": ...}`.

    `failed_pages` lists the pages whose text is missing. Use `POST /v1/extract`.
    The 0.0.1 `model` and image fields are ignored; nothing is downloaded.
    Pages are routed as `method=auto` on `/v1/extract`.
    """
    doc = extract_pdf(file.file.read(), vision=vision)
    response.headers["Deprecation"] = "true"
    return {
        "text": "\n\n".join(p.text for p in doc.pages if p.status == "ok"),
        "failed_pages": doc.failed_pages,
    }
