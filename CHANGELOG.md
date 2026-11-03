# Changelog

All notable changes to this project are recorded here. Add new entries under Unreleased.

## Unreleased

### Changed

- The project is now a `uv`-managed Python package (`pyproject.toml`, `src/llm_pdf_ocr/`) served by FastAPI. Install with `uv sync`; `src/requirements.txt` is gone. The Docker image runs `uvicorn llm_pdf_ocr.app:app` on port 5000.
- CI on GitHub-hosted runners runs ruff, pytest with an 80% coverage gate, and a Docker build. The version-badge workflow is removed.

### Added

- Docker image installs Tesseract with English data, runs as a non-root user and has a healthcheck on `/health`. CI starts the container and extracts all three fixtures through `POST /v1/extract`.

- OCR fallback (`llm_pdf_ocr.ocr`): pages without a usable text layer are rendered at 300 DPI and read with the `tesseract` command, returning text and a 0 to 1 confidence. Tesseract must be installed.

- Per-page output (`llm_pdf_ocr.extract`): `extract_pdf` reads each page from the text layer or Tesseract, and `render_markdown` / `render_json` state the method of every page. A page that fails is reported as `failed` with an error code and message, never as an empty page; the other pages are still returned.
- `POST /v1/extract`: upload a PDF and get Markdown or JSON back (`format=markdown|json`), optionally for a page range (`pages=1-3,5`). A bad, encrypted or page-less PDF and a bad page range return 400 with an error code and no stack trace. OpenAPI docs are at `/docs`.

- Vision backend settings (`llm_pdf_ocr.config`), read from `OCR_LLM_BASE_URL`, `OCR_LLM_API_KEY`, `OCR_LLM_DEFAULT_MODEL`, `OCR_LLM_ALLOWED_MODELS` (comma list), `OCR_LLM_TIMEOUT_SECONDS`, `OCR_LLM_CONCURRENCY` and `OCR_LLM_FALLBACK_TO_TESSERACT`. The backend is off unless the base URL is set; a model outside the allowed list is refused; the Tesseract fallback for a failed page is off by default. Nothing calls a model yet.
- Vision page reader (`llm_pdf_ocr.vision`): renders a page at 150 DPI and sends it to `{OCR_LLM_BASE_URL}/chat/completions` with a fixed prompt asking for Markdown with tables, returning the text, the model name and any usage numbers the endpoint reports. At most `OCR_LLM_CONCURRENCY` requests are in flight at once. Errors carry a code (`timeout`, `rate_limited`, `http_error`, `bad_response`, `empty_response`, `unreachable`) and never contain the API key, the base URL or the endpoint's own error text; redirects are refused so the key is only sent to the configured endpoint. Not yet used by `/v1/extract`.

### Security

- No request field can cause a model to be downloaded or loaded: `model` and the other 0.0.1 form fields are ignored by `/ocr` and `/v1/extract`, and a test blocks the network to prove it.

### Deprecated

- `POST /ocr` is back as an alias for users of 0.0.1. It returns `{"text": ..., "failed_pages": [...]}` and sends a `Deprecation: true` header; the old `model` and image fields are ignored and nothing is downloaded. Use `POST /v1/extract`.

### Removed

- The Microsoft TrOCR code path and its dependencies (PyTorch, Transformers, OpenCV), and `GET /models`.
