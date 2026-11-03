import json
import re
import socket
import threading
import time

import pymupdf
import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright

from llm_pdf_ocr.app import app
from llm_pdf_ocr.extract import MethodChoice, extract_pdf, render_markdown
from llm_pdf_ocr.jobs import JobStore

ATTACK = "<img src=x onerror=alert(1)>"
PAGE_ONE = "Page one says hello from the text layer of this document."
PAGE_TWO = "Page two carries on with a second paragraph of real text."


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


@pytest.fixture
def dialogs(page):
    """Every alert, confirm or prompt the page opens."""
    seen = []
    page.on("dialog", lambda dialog: (seen.append(dialog.message), dialog.dismiss()))
    return seen


def _pdf(*texts: str | None) -> bytes:
    """One page per text; `None` is a page with only an image, so no text layer."""
    doc = pymupdf.open()
    for text in texts:
        page = doc.new_page(width=400, height=300)
        if text is None:
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 30), False)
            pix.clear_with(200)
            page.insert_image(page.rect, pixmap=pix)
        else:
            page.insert_textbox(pymupdf.Rect(20, 20, 380, 280), text, fontname="helv")
    data = doc.tobytes()
    doc.close()
    return data


def _extract(page, base: str, data: bytes, method: str = "text_layer", fmt: str = "markdown"):
    page.goto(base + "/")
    page.set_input_files(
        "#file", {"name": "doc.pdf", "mimeType": "application/pdf", "buffer": data}
    )
    page.select_option("#format", fmt)
    page.select_option("#method", method)
    page.click("#submit")


def _done(page) -> None:
    expect(page.locator("#status")).to_have_text(re.compile(r"^Done"), timeout=20_000)


def test_root_loads_in_a_real_browser(live_server, page):
    response = page.goto(live_server + "/")

    assert response is not None and response.status == 200
    assert page.locator("form").count() == 1


def test_three_pages_end_to_end_each_with_its_image_method_and_failure(live_server, page):
    data = _pdf(PAGE_ONE, PAGE_TWO, None)

    _extract(page, live_server, data)
    _done(page)

    cards = page.locator(".page-result")
    expect(cards).to_have_count(3)
    for n in (1, 2, 3):
        card = cards.nth(n - 1)
        expect(card.locator("h3")).to_have_text(f"Page {n}")
        expect(card.locator(".method")).to_contain_text("text_layer")
        image = card.locator("img.page-image")
        assert re.fullmatch(rf"/v1/jobs/[\w-]+/pages/{n}\.png", image.get_attribute("src"))
        page.wait_for_function(
            "(img) => img.complete && img.naturalWidth > 0", arg=image.element_handle()
        )
    expect(cards.nth(0).locator(".text")).to_have_text(PAGE_ONE)
    expect(cards.nth(1).locator(".text")).to_have_text(PAGE_TWO)
    for ok in (0, 1):
        expect(cards.nth(ok)).not_to_have_class(re.compile("failed"))
        expect(cards.nth(ok).locator(".failure")).to_have_count(0)

    failed = cards.nth(2)
    expect(failed).to_have_class(re.compile("failed"))
    expect(failed.locator(".failure")).to_contain_text("no_text_layer")
    expect(failed.locator(".failure")).to_contain_text("the page has no usable text layer")
    expect(failed.locator(".text")).to_have_count(0)
    expect(page.locator("#status")).to_contain_text("1 failed")

    expected = render_markdown(extract_pdf(data, method=MethodChoice.TEXT_LAYER))
    assert page.locator("#result").text_content() == expected


def test_json_output_is_the_document_the_api_returns(live_server, page):
    data = _pdf(PAGE_ONE)

    _extract(page, live_server, data, fmt="json")
    _done(page)

    shown = json.loads(page.locator("#result").text_content())
    assert shown["page_count"] == 1
    assert shown["pages"][0]["text"] == PAGE_ONE
    assert shown["pages"][0]["method"] == "text_layer"


def test_progress_comes_from_the_jobs_api(live_server, page):
    polls = []

    def running_once(route):
        polls.append(route.request.url)
        if len(polls) > 1:
            return route.continue_()
        body = {"status": "running", "pages_total": 3, "pages_done": 1, "result": None}
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(re.compile(r"/v1/jobs/[\w-]+$"), running_once)
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO, PAGE_ONE))

    expect(page.locator("#status")).to_have_text("Reading pages: 1 of 3")
    expect(page.locator("#progress")).to_be_visible()
    assert page.locator("#progress").evaluate("(p) => [p.value, p.max]") == [1, 3]
    _done(page)
    expect(page.locator("#progress")).to_be_hidden()


def test_page_text_is_shown_literally_and_runs_no_script(live_server, page, dialogs):
    _extract(page, live_server, _pdf(ATTACK))
    _done(page)

    text = page.locator(".page-result .text")
    expect(text).to_have_text(ATTACK)
    assert text.locator("img").count() == 0
    assert page.locator("#results img:not(.page-image)").count() == 0
    assert ATTACK in page.locator("#result").text_content()
    assert page.locator("#result img").count() == 0
    page.wait_for_timeout(300)  # time for an injected handler to fire, if one existed
    assert dialogs == []


def test_an_expired_job_shows_an_error_never_an_empty_result(live_server, page):
    saved = app.state.jobs
    app.state.jobs = JobStore(max_jobs=3, ttl=0)  # a job expires the moment it ends
    try:
        _extract(page, live_server, _pdf(PAGE_ONE))

        status = page.locator("#status")
        expect(status).to_have_class(re.compile("error"), timeout=20_000)
        expect(status).to_contain_text("expired")
    finally:
        app.state.jobs.shutdown()
        app.state.jobs = saved
    expect(page.locator("#results")).to_be_hidden()
    expect(page.locator(".page-result")).to_have_count(0)
    expect(page.locator("#result")).to_be_hidden()
    expect(page.locator("#progress")).to_be_hidden()


def test_a_page_image_that_is_gone_says_so(live_server, page):
    page.route(re.compile(r"/pages/\d+\.png$"), lambda route: route.fulfill(status=404))
    _extract(page, live_server, _pdf(PAGE_ONE))
    _done(page)

    card = page.locator(".page-result")
    expect(card.locator(".no-image")).to_have_text("Image not available")
    expect(card.locator("img")).to_have_count(0)
    expect(card.locator(".text")).to_have_text(PAGE_ONE)
