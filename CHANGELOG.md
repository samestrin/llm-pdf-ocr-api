# Changelog

All notable changes to this project are recorded here. Add new entries under Unreleased.

## Unreleased

### Added

- README demo GIF: a real session of the web page, recorded by `uv run python docs/make_demo_gif.py`. The app runs on an ephemeral local port with nothing stubbed: "Try a sample" reads the bundled four-page PDF with the text layer and Tesseract, the find box steps through matches, and the API reference opens on `POST /v1/extract`. Frames are 1280x800 PNGs (any other size is refused), Pillow stitches them, and the script reopens the GIF to check that it animates, at one frame size, under 30 seconds. Pillow joins the dev dependency group; the recording is not part of the test run.
- Web page: a find box above the result searches the per-page text and the full Markdown or JSON output in the browser, case-insensitively. Every match is highlighted and counted, with next/previous buttons and Enter/Shift+Enter keyboard navigation.
- CI checks the API reference end to end: `tests/api_reference_check.py` loads `/docs`, every resource it names and the spec, then calls `/v1/capabilities` and `/health` at each server URL the spec declares (the root when it declares none). Pytest proves the check both ways with fixture apps, and the `try-it` job runs the same file against the compose stack, so a reference moved behind a path prefix without a `servers` entry fails CI.

### Changed

- The interactive API reference at `/docs` now uses Scalar, served by the app and working offline against `/openapi.json`. FastAPI's default documentation pages and ReDoc are replaced, and the web page and project documentation link to the new reference.

## 1.0.0 (2026-11-10)

The first release since 0.0.1: the service is rebuilt around a PDF's text layer and Tesseract, with an optional vision model you configure yourself. The version is set only in `pyproject.toml`; the package's `__version__` and the OpenAPI version are read from it, and the `.version` file is gone.

### Changed

- Web page: one set of type and spacing tokens, a visible focus ring, one card and button style, and a layout that fits 320 px wide.
- README rewritten: what the service does, a quick start (`docker run ghcr.io/samestrin/llm-pdf-ocr-api`, or build locally), a configuration table, the committed benchmark table (vision not measured) and a "Coming from 0.0.1" section. The badge says 1.0.0. Tests check the image name and that the local build-and-run commands are the ones CI's docker job runs.
- The project is now a `uv`-managed Python package (`pyproject.toml`, `src/llm_pdf_ocr/`) served by FastAPI. Install with `uv sync`; `src/requirements.txt` is gone. The Docker image runs `uvicorn llm_pdf_ocr.app:app` on port 5000.
- CI on GitHub-hosted runners runs ruff, pytest with an 80% coverage gate, and a Docker build. The version-badge workflow is removed.

### Added

- The README's web page section now shows a [screenshot](docs/screenshot-web-page-light-desktop.png) of the finished page: each page's image beside its text, and the full Markdown with copy and download buttons.
- A web page at `/`: drop or pick a PDF, choose Markdown or JSON and a method (auto, text layer, Tesseract, and the vision model when one is configured), and read the result. Plain files in `llm_pdf_ocr/web/`, no build step and no other origin; `/` is served with `Content-Security-Policy: default-src 'self'`. CI loads the page and its assets from the container with the network off.
- `OCR_WEB_PAGE=false` turns the web page off for an API-only deployment: `/` and `/static/` answer 404 `not_found`; the API is unchanged. On by default.
- `GET /v1/capabilities`: the methods a request may use and the allowed vision model names, never the endpoint URL or key. The page reads it to decide whether to offer vision.
- `GET /v1/jobs/{id}/pages/{n}.png`: each page a job reads, as a PNG (100 DPI, longer side at most 1600 px), rendered while the job runs. The images are dropped with the job's result after `OCR_JOB_TTL_SECONDS`, then the route answers 404 `job_not_found`; a page the job does not read answers 404 `page_not_found`. The uploaded PDF is still dropped when the job ends.
- The web page follows the job's progress, then shows each page's image beside its text, with the method that read it and why it failed if it did, and the full Markdown or JSON below. Page text is set as text, never as HTML, so markup in a PDF is shown literally; a job that has expired (404) is shown as an error, never as an empty result.
- `OCR_MAX_IMAGE_MB` (default 200) caps the memory page images may use, across every held job. Past it, a job still reads every page, but renders no more images: those pages answer 404 `page_not_found` and the web page shows "Image not available" beside their text. Before, up to `OCR_MAX_JOBS` × `OCR_MAX_PAGES` images could be held at once.
- The web page can copy the Markdown to the clipboard and download the result as `result.md` or `result.json`. It follows the browser's light or dark theme and has no horizontal scroll at 375 px wide.
- Release workflow (`.github/workflows/release.yml`): a `v*` tag publishes `ghcr.io/samestrin/llm-pdf-ocr-api` tagged `X.Y.Z` and `latest`, after a step that fails unless the tag equals `v` plus the `pyproject.toml` version. Only the publish job may write packages, and every action is pinned to a commit. A pull request that touches the workflow builds the image as a dry run without pushing and checks that the guard rejects a mismatched tag. CI now runs `actionlint` on the workflows.
- [Release notes draft](docs/release-notes-1.0.0.md) for the owner: what changed, coming from 0.0.1, the image name, the private first GHCR publish and the vision benchmark step. No tag is pushed.
- README "Try it" section and a CI `try-it` job that follows it.
- The README gives the owner's local Ollama steps for the vision benchmark row, and `docker-compose.yml` publishes Ollama on loopback only. A test ties the README's benchmark row to the newest result file. The vision row itself is still the owner's run.
- Web page states: empty, upload rejected and network down, each with its own message; keyboard use, accessible names, a live status and contrast checked in tests.
- README screenshots, light and dark, desktop and phone, generated by `docs/make_screenshots.py` instead of by hand.
- `benchmarks/`: twenty public-domain or freely licensed PDFs (born-digital, two-column, tables, forms, scans, poor scans) with reference text, Markdown reference tables, and a manifest of sources and licenses. A test checks every file has a source URL, a license and reference text. `benchmarks` is excluded from the Docker build context.
- Every response has an `X-Request-ID` header, and every error body carries the same `request_id`. Each request logs one line (method, path, status, time, id), never the file or any page text (the path is escaped); the 404, 405 and 422 framework errors now use the same `{"error": ...}` body (`not_found`, `method_not_allowed`, `invalid_request`); an unhandled failure is answered with 500 `internal_error` and logged by exception type only.
- Benchmark runner: `uv run python benchmarks/run.py` reports, per available method (text layer, Tesseract, and the vision model when `OCR_LLM_BASE_URL` is set), character error rate, table cell F1, seconds per page and failed pages, where a document with a failed page scores 0. Results go to a dated file in `benchmarks/results/` that names the vision model but never the endpoint URL or key.
- `docker-compose.yml` runs the API, with an optional `vision` profile that adds a local Ollama vision endpoint. CI validates the file.
- Docker image installs Tesseract with English data, runs as a non-root user and has a healthcheck on `/health`. CI starts the container and extracts all three fixtures through `POST /v1/extract`.
- OCR fallback (`llm_pdf_ocr.ocr`): pages without a usable text layer are rendered at 300 DPI and read with the `tesseract` command, returning text and a 0 to 1 confidence. Tesseract must be installed.
- Per-page output (`llm_pdf_ocr.extract`): `extract_pdf` reads each page from the text layer or Tesseract, and `render_markdown` / `render_json` state the method of every page. A page that fails is reported as `failed` with an error code and message, never as an empty page; the other pages are still returned.
- `POST /v1/extract`: upload a PDF and get Markdown or JSON back (`format=markdown|json`), optionally for a page range (`pages=1-3,5`). A bad, encrypted or page-less PDF and a bad page range return 400 with an error code and no stack trace. OpenAPI docs are at `/docs`.
- Vision backend settings (`llm_pdf_ocr.config`), read from `OCR_LLM_BASE_URL`, `OCR_LLM_API_KEY`, `OCR_LLM_DEFAULT_MODEL`, `OCR_LLM_ALLOWED_MODELS` (comma list), `OCR_LLM_TIMEOUT_SECONDS`, `OCR_LLM_CONCURRENCY` and `OCR_LLM_FALLBACK_TO_TESSERACT`. The backend is off unless the base URL is set; a model outside the allowed list is refused; the Tesseract fallback for a failed page is off by default. Nothing calls a model yet.
- Vision page reader (`llm_pdf_ocr.vision`): renders a page at 150 DPI and sends it to `{OCR_LLM_BASE_URL}/chat/completions` with a fixed prompt asking for Markdown with tables, returning the text, the model name and any usage numbers the endpoint reports. At most `OCR_LLM_CONCURRENCY` requests are in flight at once. Errors carry a code (`timeout`, `rate_limited`, `http_error`, `bad_response`, `empty_response`, `unreachable`) and never contain the API key, the base URL or the endpoint's own error text; redirects are refused so the key is only sent to the configured endpoint. Not yet used by `/v1/extract`.
- Routing per page: `POST /v1/extract` takes `method` (`auto`, the default, or `text_layer`, `tesseract`, `vision`) and `model`. `auto` uses the text layer when usable, else the vision model if one is configured, else Tesseract; with no vision backend it reads pages exactly as before. Each page now states its `model` (JSON `"model"`, Markdown `Model:`), set when the vision model read it. A `model` outside the operator's list returns 400 `model_not_allowed` (with no backend, every model is refused), and `method=vision` with no backend returns 400 `vision_not_configured`, both before any page is read. A page the vision model cannot read is `failed` with `vision_failed`; a forced `text_layer` on a page with no usable text layer is `failed` with `no_text_layer`. `POST /ocr` routes as `auto` too. A bad `OCR_LLM_*` setting now stops the service at start.
- [Vision model backend](docs/vision-backend.md) docs: every `OCR_LLM_*` setting, example settings for a local vLLM or Ollama server and for any OpenAI-compatible cloud provider (none is the default), running the service in Docker against a model on the host, and how to check it works. A test runs each example against a fake endpoint.
- Request limits from the environment: `OCR_MAX_FILE_MB` (default 50), `OCR_MAX_PAGES` (default 500, counting only the pages selected with `pages`) and `OCR_PAGE_TIMEOUT_SECONDS` (default 120, Tesseract's time per page). An upload over the size limit is cut off while it is read and answered with 413 `file_too_large`; too many selected pages get 413 `too_many_pages` before any page is read; a page that runs out of time fails with `page_timeout` while the other pages are still returned. Both routes apply the limits.
- Vision failures: a page the vision model cannot read (a timeout, HTTP 429, another error or a bad reply) is `failed` with `vision_failed` and the reason in its message. With `OCR_LLM_FALLBACK_TO_TESSERACT=true` it is read by Tesseract instead, and the page says so: a new `fallback` field in JSON (`{"method": "vision", "model": ..., "error": {...}}`, `null` otherwise) and a `Read with Tesseract because ...` line in Markdown. If Tesseract fails too, the page is `failed` with the Tesseract error and keeps its `fallback`.
- Jobs for long PDFs: `POST /v1/jobs` takes the same fields as `/v1/extract`, checks them the same way, and answers 202 with a random job id; `GET /v1/jobs/{id}` gives the status, the progress of each selected page and, when done, the result. Jobs are held in memory and run one at a time. A job holds the PDF only until it ends, and its result is dropped `OCR_JOB_TTL_SECONDS` (default 3600) later, after which the id answers 404 `job_not_found`. Over `OCR_MAX_JOBS` (default 20) held jobs, `POST /v1/jobs` answers 429 `too_many_jobs`. The service must run a single uvicorn worker, as the Docker image does.

### Security

- No request field can cause a model to be downloaded or loaded: `/ocr` ignores `model` and the other 0.0.1 form fields, `/v1/extract` refuses a `model` outside the operator's list, and a test blocks the network to prove it.

### Deprecated

- `POST /ocr` is back as an alias for users of 0.0.1. It returns `{"text": ..., "failed_pages": [...]}` and sends a `Deprecation: true` header; the old `model` and image fields are ignored and nothing is downloaded. Use `POST /v1/extract`.

### Removed

- The Microsoft TrOCR code path and its dependencies (PyTorch, Transformers, OpenCV), and `GET /models`.
- The 2024 DigitalOcean app template (`.do/`), which cloned a different repository.
- The `.version` file. The version lives in `pyproject.toml` only.
