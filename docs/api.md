# HTTP API

The interactive API reference is at `/docs` (Scalar), reading the raw spec at `/openapi.json`.

## Extract

**Endpoint:** `/v1/extract` **Method:** POST

Read a PDF page by page and return Markdown or JSON. Each page says how it was read: `text_layer` (the PDF's own text), `tesseract` (OCR, with a 0 to 1 confidence) or `vision` (a vision model, named in `model`).

- `file`: PDF file
- `format` (optional): `markdown` (default) or `json`.
- `pages` (optional): 1-based pages to read, such as `1-3,5`. Defaults to every page.
- `method` (optional): `auto` (default) uses the text layer when it is usable, else the vision model if the operator configured one, else Tesseract. `text_layer`, `tesseract` or `vision` reads every page that way.
- `model` (optional): the vision model to use, from the operator's `OCR_LLM_ALLOWED_MODELS`. Defaults to `OCR_LLM_DEFAULT_MODEL`. Any other model is refused with 400 `model_not_allowed`, and `method=vision` with no vision model configured is refused with 400 `vision_not_configured`.

```bash
curl -F file=@document.pdf -F format=json -F pages=1-2 localhost:5000/v1/extract
```

A page that could not be read is returned as `failed`, with an error code and message, and is listed in `failed_pages`; the other pages are still returned.

To read scanned pages with a vision model, local or in the cloud, see [Vision model backend](vision-backend.md). With none configured, scanned pages are read with Tesseract.

If the vision model times out, is rate limited (HTTP 429), returns an error or sends a reply that cannot be read, the page is `failed` with `vision_failed` and a message giving the reason. If the operator sets `OCR_LLM_FALLBACK_TO_TESSERACT=true` (off by default; `docker-compose.yml` turns it on unless you set it), Tesseract reads that page instead: its `method` is `tesseract` and its `fallback` (JSON) or a `Read with Tesseract because ...` line (Markdown) names the vision model and the reason it failed. `fallback` is `null` on every other page.

## Jobs

**Endpoint:** `/v1/jobs` **Method:** POST, then `/v1/jobs/{id}` **Method:** GET

For a long PDF that takes minutes to read. `POST /v1/jobs` takes the same fields as `/v1/extract` and checks them the same way, then answers at once with 202, `{"id": ..., "status": "queued"}` and a `Location` header. `GET /v1/jobs/{id}` returns the job's `status` (`queued`, `running`, `done` or `failed`), `pages_total`, `pages_done`, each selected page as `pending`, `ok` or `failed`, and, once `done`, the `result`: the Markdown text, or the JSON document `/v1/extract` would return.

```bash
curl -F file=@long.pdf -F format=json localhost:5000/v1/jobs
curl localhost:5000/v1/jobs/<id>
curl -o page1.png localhost:5000/v1/jobs/<id>/pages/1.png
```

While a job runs it renders each page it reads as a PNG (100 DPI, longer side at most 1600 px), served at `GET /v1/jobs/{id}/pages/{n}.png`. A page the job does not read, or has not rendered yet, answers 404 `page_not_found`. So does a page past `OCR_MAX_IMAGE_MB` (default 200): the images of every held job count against it together, a job that reaches it renders no more images (it still reads every page, and the web page shows "Image not available" beside that page's text), and a job's images count until it expires.

Jobs are held in the service's memory and run one at a time. The PDF is dropped when its job ends, and the result and page images `OCR_JOB_TTL_SECONDS` (default 3600) after that; then the job answers 404 `job_not_found`. At most `OCR_MAX_JOBS` (default 20) jobs are held at once, finished ones included until they expire; over that, `POST` answers 429 `too_many_jobs`. Because jobs live in memory, run a single uvicorn worker, as the Docker image does, and expect jobs to be lost on a restart.

## Web page

**Endpoint:** `/` **Method:** GET

The page for using the service from a browser, with its files under `/static/` (including the sample PDF at `/static/samples/sample.pdf`). See [The web page](web-page.md).

## Capabilities

**Endpoint:** `/v1/capabilities` **Method:** GET

Returns the methods a request may use and the vision models it may pick, for example `{"methods": ["auto", "text_layer", "tesseract"], "models": []}`. `vision` and the `OCR_LLM_ALLOWED_MODELS` names appear only when a vision model is configured. The endpoint URL and key are never returned.

## OCR (deprecated)

**Endpoint:** `/ocr` **Method:** POST

Kept for users of 0.0.1. Returns `{"text": ..., "failed_pages": [...]}`: the text of every page that was read, and the pages that failed. The 0.0.1 `model`, `threshold_value`, `kernel_width`, `kernel_height` and `min_area` fields are ignored, and no model is downloaded. Pages are routed as `method=auto` on `/v1/extract`. Use `/v1/extract` instead.

- `file`: PDF file

## Health

**Endpoint:** `/health` **Method:** GET

Returns `{"status": "ok"}`.

## Errors

A request that cannot be served returns `{"error": {"code": ..., "message": ..., "request_id": ...}}`, with no stack trace. `request_id` is also the `X-Request-ID` response header of every response and appears in the service log line for the request, which never holds the file or any page text:

- **400 Bad Request**: `invalid_pdf` (not a readable PDF, or it has no pages), `encrypted_pdf`, `invalid_page_range` or `page_out_of_range`.
- **404 Not Found**: `job_not_found`, for a job id that does not exist or has expired.
- **413 Content Too Large**: `file_too_large` or `too_many_pages`.
- **404 / 405**: `not_found` or `method_not_allowed`, for a route that does not exist or a wrong method.
- **422 Unprocessable Entity**: `invalid_request`, a missing `file` or an unknown `format`.
- **500 Internal Server Error**: `internal_error`, for a failure the service did not expect; the log names only its type.
- **429 Too Many Requests**: `too_many_jobs`.
