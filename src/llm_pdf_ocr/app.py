"""FastAPI application for llm-pdf-ocr-api."""

from fastapi import FastAPI

from llm_pdf_ocr import __version__

app = FastAPI(title="llm-pdf-ocr-api", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the service is up."""
    return {"status": "ok"}
