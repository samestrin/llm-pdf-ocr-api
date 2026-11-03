# Changelog

All notable changes to this project are recorded here. Add new entries under Unreleased.

## Unreleased

### Changed

- The project is now a `uv`-managed Python package (`pyproject.toml`, `src/llm_pdf_ocr/`) served by FastAPI. Install with `uv sync`; `src/requirements.txt` is gone. The Docker image runs `uvicorn llm_pdf_ocr.app:app` on port 5000.
- CI on GitHub-hosted runners runs ruff, pytest with an 80% coverage gate, and a Docker build. The version-badge workflow is removed.

### Added

- OCR fallback (`llm_pdf_ocr.ocr`): pages without a usable text layer are rendered at 300 DPI and read with the `tesseract` command, returning text and a 0 to 1 confidence. Tesseract must be installed.

- Per-page output (`llm_pdf_ocr.extract`): `extract_pdf` reads each page from the text layer or Tesseract, and `render_markdown` / `render_json` state the method of every page. A page that fails is reported as `failed` with an error code and message, never as an empty page; the other pages are still returned.
- `POST /v1/extract`: upload a PDF and get Markdown or JSON back (`format=markdown|json`), optionally for a page range (`pages=1-3,5`). A bad, encrypted or page-less PDF and a bad page range return 400 with an error code and no stack trace. OpenAPI docs are at `/docs`.

### Security

- No request field can cause a model to be downloaded or loaded: `model` and the other 0.0.1 form fields are ignored by `/ocr` and `/v1/extract`, and a test blocks the network to prove it.

### Deprecated

- `POST /ocr` is back as an alias for users of 0.0.1. It returns `{"text": ..., "failed_pages": [...]}` and sends a `Deprecation: true` header; the old `model` and image fields are ignored and nothing is downloaded. Use `POST /v1/extract`.

### Removed

- The Microsoft TrOCR code path and its dependencies (PyTorch, Transformers, OpenCV), and `GET /models`.
