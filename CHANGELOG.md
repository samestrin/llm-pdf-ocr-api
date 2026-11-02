# Changelog

All notable changes to this project are recorded here. Add new entries under Unreleased.

## Unreleased

### Changed

- The project is now a `uv`-managed Python package (`pyproject.toml`, `src/llm_pdf_ocr/`) served by FastAPI. Install with `uv sync`; `src/requirements.txt` is gone. The Docker image runs `uvicorn llm_pdf_ocr.app:app` on port 5000.
- CI on GitHub-hosted runners runs ruff, pytest with an 80% coverage gate, and a Docker build. The version-badge workflow is removed.

### Removed

- The Microsoft TrOCR code path and its dependencies (PyTorch, Transformers, OpenCV), and `GET /models`. `POST /ocr` is unavailable until the new extraction pipeline lands later in this release.
