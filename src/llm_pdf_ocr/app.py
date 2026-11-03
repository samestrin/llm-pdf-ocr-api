"""FastAPI application for llm-pdf-ocr-api.

A PDF that cannot be read, or a bad page range, is answered with 400 and
`{"error": {"code": ..., "message": ..., "request_id": ...}}`, never a stack trace. So is a `model`
outside the operator's list, and `method=vision` with no vision backend. A request
over a limit (upload size, pages selected) is answered with 413 and the same body.
A page that fails is reported in the result, and the other pages are still
returned. The limits come from the environment (`llm_pdf_ocr.config.load_limits`).

`POST /v1/jobs` takes the same request as `/v1/extract`, checks it the same way,
and answers 202 with a job id; `GET /v1/jobs/{id}` gives the progress per page and
the result when done. Jobs live in this process's memory (`llm_pdf_ocr.jobs`), so
the service runs a single uvicorn worker. Over `OCR_MAX_JOBS` held jobs, `POST`
answers 429; a job finished more than `OCR_JOB_TTL_SECONDS` ago is gone (404).

`GET /` is a page for using the service from a browser (files in `llm_pdf_ocr/web/`,
served with `Content-Security-Policy: default-src 'self'`, so it loads nothing from
another origin). `GET /v1/capabilities` tells it which methods and vision model names
it may offer; never the backend's URL or key.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from llm_pdf_ocr import __version__
from llm_pdf_ocr.config import (
    Limits,
    ModelNotAllowedError,
    VisionConfig,
    load_job_settings,
    load_limits,
    load_vision_config,
)
from llm_pdf_ocr.extract import (
    Document,
    MethodChoice,
    PageRangeError,
    TooManyPagesError,
    VisionNotConfiguredError,
    extract_pdf,
    render_json,
    render_markdown,
    select_pages,
)
from llm_pdf_ocr.jobs import Job, JobStore, TooManyJobsError
from llm_pdf_ocr.limits import FileTooLargeError, UploadCap, read_upload
from llm_pdf_ocr.pdf import EncryptedPdfError, PdfReadError
from llm_pdf_ocr.requestlog import RequestLog, current_request_id


@cache
def get_vision_config() -> VisionConfig:
    """The vision backend settings, read from the environment once."""
    return load_vision_config()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    get_vision_config()  # a bad OCR_LLM_* value stops the service at start, not per request
    yield
    app.state.jobs.shutdown()


app = FastAPI(
    lifespan=_lifespan,
    title="llm-pdf-ocr-api",
    version=__version__,
    description="Turn a PDF, scanned or not, into Markdown or JSON, page by page.",
)
app.state.limits = load_limits()
_job_settings = load_job_settings()
app.state.jobs = JobStore(max_jobs=_job_settings.max_jobs, ttl=_job_settings.ttl)
app.add_middleware(UploadCap)
app.add_middleware(RequestLog)  # added last, so it is outermost

WEB = Path(__file__).parent / "web"
PAGE_CSP = "default-src 'self'"
app.mount("/static", StaticFiles(directory=WEB), name="static")


class Format(StrEnum):
    MARKDOWN = "markdown"
    JSON = "json"


def _error(code: str, message: str, status_code: int = 400) -> JSONResponse:
    error = {"code": code, "message": message, "request_id": current_request_id()}
    return JSONResponse(status_code=status_code, content={"error": error})


@app.exception_handler(RequestValidationError)
def _invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = sorted({".".join(str(part) for part in e["loc"][1:]) or "body" for e in exc.errors()})
    return _error(
        "invalid_request", f"missing or invalid field: {', '.join(fields)}", status_code=422
    )


@app.exception_handler(StarletteHTTPException)
def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    response = _error(code, str(exc.detail), status_code=exc.status_code)
    response.headers.update(exc.headers or {})
    return response


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


@app.exception_handler(FileTooLargeError)
@app.exception_handler(TooManyPagesError)
def _over_a_limit(request: Request, exc: FileTooLargeError | TooManyPagesError) -> JSONResponse:
    return _error(exc.code, str(exc), status_code=413)


@app.exception_handler(TooManyJobsError)
def _too_many_jobs(request: Request, exc: TooManyJobsError) -> JSONResponse:
    return _error(exc.code, str(exc), status_code=429)


def _limits(request: Request) -> Limits:
    return request.app.state.limits


PdfFile = Annotated[UploadFile, File(description="The PDF.")]
FormatField = Annotated[Format, Form(description="`markdown` or `json`.")]
PagesField = Annotated[
    str | None, Form(description="1-based pages to read, such as `1-3,5`. Default: all.")
]
MethodField = Annotated[
    MethodChoice,
    Form(
        description="`auto` (default): the text layer when usable, else the vision model "
        "if configured, else Tesseract. Or `text_layer`, `tesseract` or `vision` for "
        "every page."
    ),
]
ModelField = Annotated[
    str | None,
    Form(description="Vision model, from the operator's list. Default: the operator's."),
]


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the service is up."""
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def page() -> FileResponse:
    """The web page: pick a PDF, choose output and method, read the result."""
    return FileResponse(WEB / "index.html", headers={"Content-Security-Policy": PAGE_CSP})


@app.get("/v1/capabilities")
def capabilities(vision: Vision) -> dict[str, list[str]]:
    """The methods this service offers, and the vision model names a caller may pick.

    `vision` is listed only when a vision model is configured. Nothing else about the
    backend is given: no URL and no key.
    """
    methods = [MethodChoice.AUTO, MethodChoice.TEXT_LAYER, MethodChoice.TESSERACT]
    if vision.enabled:
        methods.append(MethodChoice.VISION)
    return {"methods": [m.value for m in methods], "models": list(vision.allowed_models)}


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
        413: {"description": "The file is too large, or too many pages are selected."},
    },
)
def extract(
    request: Request,
    file: PdfFile,
    format: FormatField = Format.MARKDOWN,
    pages: PagesField = None,
    method: MethodField = MethodChoice.AUTO,
    model: ModelField = None,
    *,
    vision: Vision,
) -> Response:
    """Extract a PDF's text page by page: from the text layer, a vision model or Tesseract."""
    limits = _limits(request)
    doc = extract_pdf(
        read_upload(file.file, limits.max_file_bytes),
        pages=pages,
        method=method,
        model=model,
        vision=vision,
        max_pages=limits.max_pages,
        page_timeout=limits.page_timeout,
    )
    if format is Format.JSON:
        return Response(render_json(doc), media_type="application/json")
    return Response(render_markdown(doc), media_type="text/markdown; charset=utf-8")


@app.post("/ocr", deprecated=True)
def ocr(
    request: Request,
    file: Annotated[UploadFile, File(description="The PDF.")],
    response: Response,
    vision: Vision,
) -> dict:
    """Deprecated alias kept for 0.0.1: the whole PDF's text as `{"text": ...}`.

    `failed_pages` lists the pages whose text is missing. Use `POST /v1/extract`.
    The 0.0.1 `model` and image fields are ignored; nothing is downloaded.
    Pages are routed as `method=auto` on `/v1/extract`.
    """
    limits = _limits(request)
    doc = extract_pdf(
        read_upload(file.file, limits.max_file_bytes),
        vision=vision,
        max_pages=limits.max_pages,
        page_timeout=limits.page_timeout,
    )
    response.headers["Deprecation"] = "true"
    return {
        "text": "\n\n".join(p.text for p in doc.pages if p.status == "ok"),
        "failed_pages": doc.failed_pages,
    }


@app.post(
    "/v1/jobs",
    status_code=202,
    responses={
        202: {"description": "The job is queued. `Location` is where to follow it."},
        400: {"description": "As for `/v1/extract`."},
        413: {"description": "As for `/v1/extract`."},
        429: {"description": "The service holds as many jobs as it may. Try again later."},
    },
)
def create_job(
    request: Request,
    response: Response,
    file: PdfFile,
    format: FormatField = Format.MARKDOWN,
    pages: PagesField = None,
    method: MethodField = MethodChoice.AUTO,
    model: ModelField = None,
    *,
    vision: Vision,
) -> dict:
    """Queue a PDF to be read like `/v1/extract`, and answer at once with the job's id.

    The request is checked first, so a bad PDF, page range, model or a request over
    a limit is refused here and no job is made.
    """
    limits = _limits(request)
    data = read_upload(file.file, limits.max_file_bytes)
    select_pages(
        data, pages=pages, method=method, model=model, vision=vision, max_pages=limits.max_pages
    )

    def work(data: bytes, job: Job) -> Document:
        return extract_pdf(
            data,
            pages=pages,
            method=method,
            model=model,
            vision=vision,
            max_pages=limits.max_pages,
            page_timeout=limits.page_timeout,
            progress=job,
        )

    job = request.app.state.jobs.submit(data, work, format=format)
    response.headers["Location"] = f"/v1/jobs/{job.id}"
    return {"id": job.id, "status": job.status}


@app.get(
    "/v1/jobs/{job_id}",
    responses={404: {"description": "No job with this id, or it has expired."}},
)
def get_job(request: Request, job_id: str) -> Response:
    """A job's status and progress per page; `result` is set once it is `done`.

    `result` is the Markdown text, or the JSON document `/v1/extract` would return.
    A `failed` job has an `error`. Once a job has been finished for
    `OCR_JOB_TTL_SECONDS`, it and its result are gone.
    """
    job = request.app.state.jobs.get(job_id)
    if job is None:
        return _error("job_not_found", "no job with this id, or it has expired", 404)
    result = job.result
    if result is not None:
        if job.options["format"] is Format.JSON:
            result = json.loads(render_json(result))
        else:
            result = render_markdown(result)
    pages = sorted(job.pages.items())
    return JSONResponse(
        {
            "id": job.id,
            "status": job.status,
            "format": job.options["format"],
            "pages_total": job.pages_total,
            "pages_done": sum(status != "pending" for _, status in pages),
            "pages": [{"page": n, "status": status} for n, status in pages],
            "result": result,
            "error": job.error,
        }
    )
