import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from llm_pdf_ocr.app import app


@pytest.fixture(scope="session")
def live_server():
    """The app on a free local port, for tests that need a real browser."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser.new_page()
        browser.close()


def test_root_loads_in_a_real_browser(live_server, page):
    response = page.goto(live_server + "/")

    assert response is not None and response.status == 200
    assert page.locator("form").count() == 1
