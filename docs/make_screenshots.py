"""Remake the README's web page screenshots.

Run from the repository root (chromium installed with `uv run playwright install chromium`):

    uv run python docs/make_screenshots.py [OUT_DIR]

OUT_DIR defaults to docs/. Writes screenshot-web-page-{light,dark}-{desktop,phone}.png (a
finished two-page result) and screenshot-web-page-empty-light-desktop.png (the page before
a file is chosen).

The page and its files come from the real app; the jobs API is stubbed in the browser with
a fixed result (one page read from the text layer, one by Tesseract), so the images are the
same on every machine and need neither Tesseract nor a model.
"""

import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

import pymupdf
import uvicorn
from playwright.sync_api import expect, sync_playwright

from llm_pdf_ocr.app import app
from llm_pdf_ocr.extract import Document, Method, Page, render_json

SIZES = {"desktop": {"width": 1440, "height": 900}, "phone": {"width": 390, "height": 844}}
SCHEMES = ("light", "dark")
JOB_ID = "screenshot"
PAGES = (
    "Invoice 2041. Thank you for your order. Payment is due within thirty days.",
    "Terms: goods remain our property until paid in full. Questions? Write to us.",
)


def make_pdf() -> bytes:
    doc = pymupdf.open()
    for text in PAGES:
        page = doc.new_page(width=400, height=300)
        page.insert_textbox(pymupdf.Rect(20, 20, 380, 280), text, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def page_images(data: bytes) -> list[bytes]:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return [page.get_pixmap(dpi=100).tobytes("png") for page in doc]


def job_body() -> str:
    """The finished job the stubbed `GET /v1/jobs/{id}` returns."""
    doc = Document(
        pages=[
            Page(number=1, status="ok", method=Method.TEXT_LAYER, text=PAGES[0]),
            Page(number=2, status="ok", method=Method.TESSERACT, text=PAGES[1], confidence=0.94),
        ]
    )
    return json.dumps(
        {
            "id": JOB_ID,
            "status": "done",
            "format": "json",
            "pages_total": 2,
            "pages_done": 2,
            "pages": [{"page": 1, "status": "ok"}, {"page": 2, "status": "ok"}],
            "result": json.loads(render_json(doc)),
            "error": None,
        }
    )


def stub_api(page, images: list[bytes]) -> None:
    created = json.dumps({"id": JOB_ID, "status": "queued"})
    page.route(
        re.compile(r"/v1/jobs$"),
        lambda route: route.fulfill(status=202, content_type="application/json", body=created),
    )
    body = job_body()
    page.route(
        re.compile(rf"/v1/jobs/{JOB_ID}$"),
        lambda route: route.fulfill(status=200, content_type="application/json", body=body),
    )

    def image(route):
        n = int(re.search(r"/pages/(\d+)\.png$", route.request.url)[1])
        route.fulfill(status=200, content_type="image/png", body=images[n - 1])

    page.route(re.compile(r"/pages/\d+\.png$"), image)


def start_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    return server, thread, f"http://127.0.0.1:{port}"


def main(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    data = make_pdf()
    images = page_images(data)
    server, thread, base = start_server()
    written = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            context = browser.new_context(viewport=SIZES["desktop"], color_scheme="light")
            page = context.new_page()
            page.goto(base + "/")
            path = out / "screenshot-web-page-empty-light-desktop.png"
            page.screenshot(path=str(path), full_page=True)
            written.append(path)
            context.close()

            for scheme in SCHEMES:
                for name, size in SIZES.items():
                    context = browser.new_context(viewport=size, color_scheme=scheme)
                    page = context.new_page()
                    stub_api(page, images)
                    page.goto(base + "/")
                    page.set_input_files(
                        "#file",
                        {"name": "invoice.pdf", "mimeType": "application/pdf", "buffer": data},
                    )
                    page.click("#submit")
                    expect(page.locator("#status")).to_have_text(
                        re.compile(r"^Done"), timeout=20_000
                    )
                    page.wait_for_function(
                        "() => [...document.images].every((i) => i.complete && i.naturalWidth > 0)"
                    )
                    path = out / f"screenshot-web-page-{scheme}-{name}.png"
                    page.screenshot(path=str(path), full_page=True)
                    written.append(path)
                    context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    return written


if __name__ == "__main__":
    for path in main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent):
        print(path)
