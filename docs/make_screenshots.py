"""Remake the README's web page screenshots.

Run from the repository root (chromium installed with `uv run playwright install chromium`):

    uv run python docs/make_screenshots.py [OUT_DIR]

OUT_DIR defaults to docs/. Writes screenshot-web-page-{light,dark}-{desktop,phone}.png.
"""

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

SIZES = {"desktop": {"width": 1100, "height": 900}, "phone": {"width": 390, "height": 844}}
SCHEMES = ("light", "dark")
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
    server, thread, base = start_server()
    written = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            for scheme in SCHEMES:
                for name, size in SIZES.items():
                    context = browser.new_context(viewport=size, color_scheme=scheme)
                    page = context.new_page()
                    page.goto(base + "/")
                    page.set_input_files(
                        "#file",
                        {
                            "name": "invoice.pdf",
                            "mimeType": "application/pdf",
                            "buffer": make_pdf(),
                        },
                    )
                    page.select_option("#method", "text_layer")
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
