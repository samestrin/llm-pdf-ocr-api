# Changelog

All notable changes to this project are recorded here. Add new entries under Unreleased.

## Unreleased

### Changed

- The project is now a `uv`-managed Python package (`pyproject.toml`, `src/llm_pdf_ocr/`) served by FastAPI. Install with `uv sync`; `src/requirements.txt` is gone. The Docker image runs `uvicorn llm_pdf_ocr.app:app` on port 5000.
- CI on GitHub-hosted runners runs ruff, pytest with an 80% coverage gate, and a Docker build. The version-badge workflow is removed.

### Added

- OCR fallback (`llm_pdf_ocr.ocr`): pages without a usable text layer are rendered at 300 DPI and read with the `tesseract` command, returning text and a 0 to 1 confidence. Tesseract must be installed.

- Per-page output (`llm_pdf_ocr.extract`): `extract_pdf` reads each page from the text layer or Tesseract, and `render_markdown` / `render_json` state the method of every page. A page that fails is reported as `failed` with an error code and message, never as an empty page; the other pages are still returned.

### Removed

- The Microsoft TrOCR code path and its dependencies (PyTorch, Transformers, OpenCV), and `GET /models`. `POST /ocr` is unavailable until the new extraction pipeline lands later in this release.
