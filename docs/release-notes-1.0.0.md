# Release notes: 1.0.0 (draft for the owner)

Status: draft. No tag has been pushed. The tag and the public post are the owner's.

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

## Before announcing

1. **Benchmark against a vision model.** The committed results in `benchmarks/results/` do not measure vision. Run it once and commit the result file:

   ```sh
   export OCR_LLM_BASE_URL=...        # an OpenAI-compatible endpoint
   export OCR_LLM_API_KEY=...
   export OCR_LLM_DEFAULT_MODEL=...
   uv run python benchmarks/run.py
   git add benchmarks/results/ && git commit -m "Benchmark results with a vision model"
   ```

   The result file names the model but never the endpoint URL or key. Update the benchmark table in the README from it.
2. **Check the version.** `pyproject.toml` must say `1.0.0`, and the CHANGELOG "Unreleased" entries should be moved under 1.0.0 if the owner wants them in this release.
3. **Tag.** `git tag v1.0.0 && git push origin v1.0.0` publishes the image.
4. **Make the package public.** The first publish to GHCR is private. Until the owner sets the package visibility to public (GitHub, Packages, `llm-pdf-ocr-api`, Package settings, Change visibility), `docker run ghcr.io/samestrin/llm-pdf-ocr-api` fails for everyone else with "denied". Check by pulling from a machine that is not logged in to GHCR.
5. **Post.** Announcement text is the owner's.
