# Release notes: 1.0.0 (draft for the owner)

Status: ready for the owner. The version and CHANGELOG are done (CHANGELOG dated 2026-11-10); no tag has been pushed. The tag, the public package and the post are the owner's.

## Image

`ghcr.io/samestrin/llm-pdf-ocr-api`, tagged `1.0.0` and `latest`, published by `.github/workflows/release.yml` when a `v*` tag is pushed. The workflow fails unless the tag equals `v` plus the version in `pyproject.toml` (`v1.0.0`).

```sh
docker run -p 5000:5000 ghcr.io/samestrin/llm-pdf-ocr-api
```

## What changed

1.0.0 is the first release since 0.0.1. The service is rebuilt around a PDF's text layer and Tesseract, with an optional vision model you configure yourself (see [vision-backend.md](vision-backend.md)).

- `POST /v1/extract` reads a PDF page by page and returns Markdown or JSON. Each page says how it was read (`text_layer`, `tesseract` or `vision`); a page that fails is reported as `failed`, never as an empty page. Options: `format`, `pages`, `method`, `model`.
- Optional vision backend for scans, set with `OCR_LLM_*` variables. Off unless `OCR_LLM_BASE_URL` is set; models outside `OCR_LLM_ALLOWED_MODELS` are refused; a failed page can fall back to Tesseract.
- `POST /v1/jobs` and `GET /v1/jobs/{id}` for long PDFs (in memory, single worker).
- Request limits (`OCR_MAX_FILE_MB`, `OCR_MAX_PAGES`, `OCR_PAGE_TIMEOUT_SECONDS`), `X-Request-ID` on every response, one log line per request with no file contents.
- Docker image with Tesseract, non-root user, healthcheck; `docker-compose.yml` with an optional `vision` profile.
- Benchmark set and runner in `benchmarks/` (CER, table cell F1, seconds per page, failed pages).
- A `uv`-managed Python package served by FastAPI; CI runs ruff, pytest (80% coverage gate) and a Docker build; release workflow publishes to GHCR.

Full list: [CHANGELOG.md](../CHANGELOG.md).

## Coming from 0.0.1

- Use `POST /v1/extract` instead of `/ocr`. `/ocr` still works but is deprecated (it sends a `Deprecation: true` header), and its `model`, `threshold_value`, `kernel_width`, `kernel_height` and `min_area` fields are ignored.
- No model is downloaded and no GPU is needed. TrOCR, PyTorch, Transformers and OpenCV are gone; `GET /models` is gone.
- The server is uvicorn on port 5000 (it was gunicorn) and runs as a non-root user. Run one worker only, because jobs are held in memory.
- Install with `uv sync`; `src/requirements.txt` and `.version` are gone.

## Done in the repository

1. **Version and CHANGELOG.** `pyproject.toml` says `1.0.0`, and the CHANGELOG's former Unreleased entries are merged into one dated `1.0.0` section; Unreleased is empty. A test checks the top version equals `pyproject.toml`'s.
2. **Benchmark steps prepared.** The README gives the local Ollama steps for the vision benchmark row, and `docker-compose.yml` publishes Ollama on loopback only. The committed results in `benchmarks/results/` still do not measure vision: that run was not done, and the README table says so until it is.
3. **Web page, "Try it" and screenshots.** The page has empty, rejected-upload and network-down states, and the README has a "Try it" section and light/dark, desktop/phone screenshots made by `docs/make_screenshots.py`.

## Owner steps

1. **Benchmark against a vision model.** Follow the README's steps, run `uv run python benchmarks/run.py` with `OCR_LLM_BASE_URL`, `OCR_LLM_API_KEY` and `OCR_LLM_DEFAULT_MODEL` set, commit the result file and update the README row. The result file names the model but never the endpoint URL or key.
2. **Tag.** `git tag v1.0.0 && git push origin v1.0.0` publishes the image.
3. **Make the package public.** The first publish to GHCR is private. Until the owner sets the package visibility to public (GitHub, Packages, `llm-pdf-ocr-api`, Package settings, Change visibility), `docker run ghcr.io/samestrin/llm-pdf-ocr-api` fails for everyone else with "denied". Check by pulling from a machine that is not logged in to GHCR.
4. **Post.** Announcement text is the owner's.
