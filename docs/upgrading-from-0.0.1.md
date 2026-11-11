# Coming from 0.0.1

0.0.1 was a Flask service that ran TrOCR on CUDA. 1.0.0 is a different service with the same repository:

- Use `POST /v1/extract` instead of `/ocr`. It returns Markdown or JSON, says how each page was read and lists failed pages.
- `/ocr` still works but is deprecated. It returns `{"text": ..., "failed_pages": [...]}`, and its `model`, `threshold_value`, `kernel_width`, `kernel_height` and `min_area` fields are now ignored.
- No model is downloaded and no GPU is needed. TrOCR, PyTorch and Transformers are gone; scanned pages are read with Tesseract, or with a vision model if you configure one.
- The server is uvicorn on port 5000 (it was gunicorn), and runs as a non-root user. Run one worker only, because jobs are held in memory.
- Install with `uv sync`; `src/requirements.txt` is gone.
