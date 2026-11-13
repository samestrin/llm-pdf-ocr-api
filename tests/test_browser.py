import base64
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
from llm_pdf_ocr.extract import (
    Document,
    ErrorCode,
    Fallback,
    Method,
    MethodChoice,
    Page,
    PageError,
    extract_pdf,
    render_json,
    render_markdown,
)
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
        expect(card.locator(".method")).to_have_text("Method: Text layer")
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

    expect(page.locator("#status")).to_have_text("Reading page 2 of 3…")
    expect(page.locator("#status .thinking-dot")).to_have_count(3)
    expect(page.locator("#progress")).to_be_visible()
    assert page.locator("#progress").evaluate("(p) => [p.value, p.max]") == [1, 3]
    _done(page)
    expect(page.locator("#progress")).to_be_hidden()


def _first_poll(page, body: dict) -> None:
    """Answer the first `GET /v1/jobs/{id}` with `body`; later polls reach the server."""
    polls = []

    def handle(route):
        polls.append(route.request.url)
        if len(polls) > 1:
            return route.continue_()
        return route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(re.compile(r"/v1/jobs/[\w-]+$"), handle)


def test_progress_is_indeterminate_until_the_job_knows_its_pages(live_server, page):
    # A first job leaves the bar at a value; the second must not show that stale value.
    _first_poll(page, {"status": "running", "pages_total": 3, "pages_done": 2, "result": None})
    _extract(page, live_server, _pdf(PAGE_ONE))
    _done(page)
    page.unroute_all()
    _first_poll(page, {"status": "running", "pages_total": None, "pages_done": 0, "result": None})
    page.click("#submit")

    expect(page.locator("#status")).to_have_text("Reading…")
    expect(page.locator("#progress")).to_be_visible()
    assert page.locator("#progress").get_attribute("value") is None
    _done(page)


def test_a_failed_job_shows_its_message_and_no_result(live_server, page):
    error = {"code": "job_failed", "message": "the job could not be completed"}
    _first_poll(page, {"status": "failed", "pages_total": 1, "pages_done": 1, "error": error})
    _extract(page, live_server, _pdf(PAGE_ONE))

    status = page.locator("#status")
    expect(status).to_have_text("the job could not be completed")
    expect(status).to_have_class(re.compile("error"))
    expect(page.locator("#results")).to_be_hidden()
    expect(page.locator("#progress")).to_be_hidden()
    expect(page.locator("#submit")).to_be_enabled()


def test_model_confidence_and_fallback_are_shown_as_render_markdown_shows_them(live_server, page):
    vision_failed = PageError(code=ErrorCode.VISION_FAILED, message="the model timed out")
    doc = Document(
        pages=[
            Page(number=1, status="ok", method=Method.VISION, model="m-one", text=PAGE_ONE),
            Page(
                number=2,
                status="ok",
                method=Method.TESSERACT,
                text=PAGE_TWO,
                confidence=0.87,
                fallback=Fallback(method=Method.VISION, model="m-one", error=vision_failed),
            ),
        ]
    )
    _first_poll(
        page,
        {
            "status": "done",
            "pages_total": 2,
            "pages_done": 2,
            "result": json.loads(render_json(doc)),
            "error": None,
        },
    )
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
    _done(page)

    cards = page.locator(".page-result")
    expect(cards.nth(0).locator(".method")).to_have_text("Method: Vision model · Model: m-one")
    expect(cards.nth(0).locator(".fallback")).to_have_count(0)
    expect(cards.nth(1).locator(".method")).to_have_text("Method: Tesseract · Confidence: 0.87")
    expect(cards.nth(1).locator(".fallback")).to_have_text(
        "Read with Tesseract because vision (m-one) failed (vision_failed): the model timed out"
    )
    assert page.locator("#result").text_content() == render_markdown(doc)


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
    expect(page.locator("#submit")).to_be_enabled()


def test_a_page_image_that_is_gone_says_so(live_server, page):
    page.route(re.compile(r"/pages/\d+\.png$"), lambda route: route.fulfill(status=404))
    _extract(page, live_server, _pdf(PAGE_ONE))
    _done(page)

    card = page.locator(".page-result")
    expect(card.locator(".no-image")).to_have_text("Image not available")
    expect(card.locator("img")).to_have_count(0)
    expect(card.locator(".text")).to_have_text(PAGE_ONE)


def test_a_page_past_the_image_budget_says_so_and_shows_its_text(live_server, page):
    saved = app.state.jobs
    app.state.jobs = JobStore(max_jobs=3, ttl=60, max_image_bytes=1)  # no page image fits
    try:
        _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
        _done(page)
    finally:
        app.state.jobs.shutdown()
        app.state.jobs = saved

    cards = page.locator(".page-result")
    expect(cards).to_have_count(2)
    for n, text in enumerate((PAGE_ONE, PAGE_TWO)):
        expect(cards.nth(n).locator(".no-image")).to_have_text("Image not available")
        expect(cards.nth(n).locator("img")).to_have_count(0)
        expect(cards.nth(n).locator(".text")).to_have_text(text)


def _result_doc(live_server, page, fmt="markdown"):
    data = _pdf(PAGE_ONE, PAGE_TWO)
    _extract(page, live_server, data, fmt=fmt)
    _done(page)
    return extract_pdf(data, method=MethodChoice.TEXT_LAYER)


def test_copy_puts_the_markdown_on_the_clipboard(live_server, page):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server)
    doc = _result_doc(live_server, page, fmt="json")  # copy is Markdown whatever is shown
    page.click("#copy")

    expect(page.locator("#status")).to_have_text("Markdown copied.")
    assert page.evaluate("navigator.clipboard.readText()") == render_markdown(doc)


def test_each_download_delivers_a_file_with_the_matching_content(live_server, page):
    doc = _result_doc(live_server, page)

    with page.expect_download() as info:
        page.click("#download-md")
    md = info.value
    assert md.suggested_filename == "result.md"
    assert open(md.path(), encoding="utf-8").read() == render_markdown(doc)

    with page.expect_download() as info:
        page.click("#download-json")
    js = info.value
    assert js.suggested_filename == "result.json"
    assert json.load(open(js.path(), encoding="utf-8")) == json.loads(render_json(doc))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_both_themes_apply(live_server, page, scheme):
    page.emulate_media(color_scheme=scheme)
    page.goto(live_server + "/")
    background = page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert background == {"light": "rgb(244, 245, 247)", "dark": "rgb(15, 17, 21)"}[scheme]


def test_no_horizontal_scroll_at_phone_width(live_server, page):
    page.set_viewport_size({"width": 375, "height": 800})
    _extract(page, live_server, _pdf(PAGE_ONE, "x" * 400))
    _done(page)

    widths = page.evaluate(
        "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    )
    assert widths[0] <= widths[1] == 375


_WIDTHS = "[document.documentElement.scrollWidth, document.documentElement.clientWidth]"
CONTROLS = {
    "theme-toggle",
    "file",
    "sample",
    "format",
    "method",
    "submit",
    "copy",
    "download-md",
    "download-json",
}


def test_no_horizontal_scroll_at_the_narrowest_phone_width(live_server, page):
    page.set_viewport_size({"width": 320, "height": 640})
    page.goto(live_server + "/")
    empty = page.evaluate(_WIDTHS)
    assert empty[0] <= empty[1] == 320

    _extract(page, live_server, _pdf(PAGE_ONE, "x" * 400))
    _done(page)
    done = page.evaluate(_WIDTHS)
    assert done[0] <= done[1] == 320


# The element that shows the ring: the file input is visually hidden, so its drop zone does.
_RING = """() => {
  const focused = document.activeElement;
  const shown = focused.id === "file" ? focused.closest(".drop") : focused;
  const style = getComputedStyle(shown);
  return {id: focused.id, style: style.outlineStyle, width: parseFloat(style.outlineWidth)};
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_every_control_shows_a_focus_ring_when_reached_by_keyboard(live_server, page, scheme):
    page.emulate_media(color_scheme=scheme)
    _extract(page, live_server, _pdf(PAGE_ONE))
    _done(page)

    rings = {}
    for _ in range(20):
        page.keyboard.press("Tab")
        ring = page.evaluate(_RING)
        if ring["id"] in CONTROLS:
            rings[ring["id"]] = ring

    assert set(rings) == CONTROLS
    for ring in rings.values():
        assert ring["style"] not in ("none", "auto") and ring["width"] >= 2, ring


def test_before_a_file_is_chosen_the_page_says_what_to_do_and_the_limits(live_server, page):
    page.goto(live_server + "/")

    empty = page.locator("#empty")
    expect(empty).to_be_visible()
    expect(empty).to_contain_text("Extract")
    expect(page.locator(".drop-hint")).to_have_text(re.compile(r"50 MB.*500 pages"))
    expect(page.locator("#status")).to_be_hidden()
    expect(page.locator("#results")).to_be_hidden()

    page.set_input_files(
        "#file", {"name": "doc.pdf", "mimeType": "application/pdf", "buffer": b"x"}
    )
    expect(empty).to_be_hidden()


def test_a_rejected_upload_shows_the_apis_message_and_request_id(live_server, page):
    with page.expect_response(re.compile(r"/v1/jobs$")) as info:
        _extract(page, live_server, b"this is not a pdf")
    response = info.value
    assert response.status == 400
    error = response.json()["error"]

    status = page.locator("#status")
    expect(status).to_have_class(re.compile("error"))
    expect(status).to_contain_text(error["message"])
    expect(status).to_contain_text(f"Request ID: {error['request_id']}")
    expect(page.locator("#results")).to_be_hidden()
    expect(page.locator("#submit")).to_be_enabled()


@pytest.mark.parametrize("path", [r"/v1/jobs$", r"/v1/jobs/[\w-]+$"])
def test_a_network_failure_shows_a_plain_message(live_server, page, path):
    page.route(re.compile(path), lambda route: route.abort("internetdisconnected"))
    _extract(page, live_server, _pdf(PAGE_ONE))

    status = page.locator("#status")
    expect(status).to_have_text("Could not reach the service. Check the connection and try again.")
    expect(status).to_have_class(re.compile("error"))
    expect(page.locator("#results")).to_be_hidden()
    expect(page.locator("#progress")).to_be_hidden()
    expect(page.locator("#submit")).to_be_enabled()


def _tab_to(page, element_id: str) -> None:
    for _ in range(20):
        page.keyboard.press("Tab")
        if page.evaluate("document.activeElement.id") == element_id:
            return
    raise AssertionError(f"#{element_id} is not reachable with Tab")


def test_the_whole_flow_works_with_the_keyboard_alone(live_server, page):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=live_server)
    data = _pdf(PAGE_ONE, PAGE_TWO)
    page.goto(live_server + "/")

    _tab_to(page, "file")
    with page.expect_file_chooser() as chooser:
        page.keyboard.press("Space")
    chooser.value.set_files({"name": "doc.pdf", "mimeType": "application/pdf", "buffer": data})
    expect(page.locator("#drop-text")).to_have_text("doc.pdf")

    _tab_to(page, "format")
    page.keyboard.press("ArrowDown")  # Markdown -> JSON
    expect(page.locator("#format")).to_have_value("json")
    _tab_to(page, "method")
    page.keyboard.press("ArrowDown")  # Auto -> Text layer
    expect(page.locator("#method")).to_have_value("text_layer")
    _tab_to(page, "submit")
    page.keyboard.press("Enter")
    _done(page)
    assert json.loads(page.locator("#result").text_content())["page_count"] == 2

    doc = extract_pdf(data, method=MethodChoice.TEXT_LAYER)
    _tab_to(page, "copy")
    page.keyboard.press("Enter")
    expect(page.locator("#status")).to_have_text("Markdown copied.")
    assert page.evaluate("navigator.clipboard.readText()") == render_markdown(doc)
    for button, name in (("download-md", "result.md"), ("download-json", "result.json")):
        _tab_to(page, button)
        with page.expect_download() as info:
            page.keyboard.press("Space")
        assert info.value.suggested_filename == name


_DARK = "matchMedia('(prefers-color-scheme: dark)').matches"


def test_every_control_has_an_accessible_name(live_server, page):
    page.route(
        "**/v1/capabilities",
        lambda route: route.fulfill(
            json={"methods": ["auto", "text_layer", "tesseract", "vision"], "models": ["m-one"]}
        ),
    )
    _extract(page, live_server, _pdf(PAGE_ONE), method="vision")
    expect(page.locator("#model")).to_be_visible()
    page.unroute_all()
    page.select_option("#method", "text_layer")
    page.click("#submit")
    _done(page)
    page.select_option("#method", "vision")  # the model picker is shown again

    controls = page.locator("input, select, button")
    ids = [controls.nth(n).get_attribute("id") for n in range(controls.count())]
    assert set(ids) == CONTROLS | {"model", "find-input", "find-prev", "find-next"}
    names = {
        "theme-toggle": "Light theme" if page.evaluate(_DARK) else "Dark theme",
        "file": "PDF file",
        "sample": "Try a sample",
        "format": "Output",
        "method": "Method",
        "model": "Model",
        "submit": "Extract",
        "find-input": "Find in result",
        "find-prev": "Previous match",
        "find-next": "Next match",
        "copy": "Copy Markdown",
        "download-md": "Download Markdown",
        "download-json": "Download JSON",
    }
    for element_id, name in names.items():
        expect(page.locator(f"#{element_id}")).to_have_accessible_name(name)


def test_status_changes_are_announced_and_results_are_not_read_out(live_server, page):
    page.goto(live_server + "/")

    status = page.locator("#status")
    assert status.get_attribute("role") == "status"
    assert status.get_attribute("aria-live") == "polite"
    # A whole document in a live region would be read out in one go.
    assert page.locator("[aria-live] #results, [aria-live] #empty").count() == 0


# The WCAG contrast ratio of every visible piece of the page's own text against the
# colour behind it (walking up to the first ancestor with a background).
_CONTRAST = """(selectors) => {
  const rgba = (s) => {
    const [r, g, b, a = 1] = s.match(/[\\d.]+/g).map(Number);
    return [r, g, b, a];
  };
  const over = (top, under) => top.slice(0, 3).map((c, i) => c * top[3] + under[i] * (1 - top[3]));
  const lum = (rgb) => {
    const [r, g, b] = rgb.map((c) => {
      c /= 255;
      return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  };
  const background = (node) => {
    const layers = [];
    for (let n = node; n; n = n.parentElement) {
      const bg = rgba(getComputedStyle(n).backgroundColor);
      if (bg[3] > 0) layers.push(bg);
      if (bg[3] === 1) break;
    }
    let colour = [255, 255, 255];
    for (const layer of layers.reverse()) colour = over(layer, colour);
    return colour;
  };
  const own = (node) =>
    [...node.childNodes].some((c) => c.nodeType === Node.TEXT_NODE && c.textContent.trim());
  const nodes = [...document.querySelectorAll("body *")].filter(
    (n) => (own(n) || n.matches(selectors)) && n.checkVisibility() && !n.disabled
      && !n.closest("option, script, style")
  );
  return nodes.map((n) => {
    const bg = background(n);
    const fg = over(rgba(getComputedStyle(n).color), bg);
    const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    return { what: n.id || n.className || n.tagName, ratio: (hi + 0.05) / (lo + 0.05) };
  });
}"""


def _contrast(page) -> list[dict]:
    return page.evaluate(_CONTRAST, "select")


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_pages_own_text_meets_wcag_aa_contrast_in_both_themes(live_server, page, scheme):
    page.emulate_media(color_scheme=scheme)
    page.goto(live_server + "/")
    seen = _contrast(page)  # the empty state

    _extract(page, live_server, b"this is not a pdf")  # the error state
    expect(page.locator("#status")).to_have_class(re.compile("error"))
    seen += _contrast(page)

    _extract(page, live_server, _pdf(PAGE_ONE, None))  # a result with a failed page
    _done(page)
    seen += _contrast(page)
    page.hover("#drop")
    seen += _contrast(page)

    whats = {s["what"] for s in seen}
    assert {"status", "drop-hint", "empty", "submit", "failure", "method", "text"} <= whats
    low = [s for s in seen if s["ratio"] < 4.5]
    assert low == [], low


@pytest.fixture
def fresh_jobs():
    """An empty job store, so these tests do not depend on how many jobs earlier ones left."""
    saved = app.state.jobs
    app.state.jobs = JobStore(max_jobs=3, ttl=60)
    yield
    app.state.jobs.shutdown()
    app.state.jobs = saved


def _background(page) -> str:
    return page.evaluate("getComputedStyle(document.body).backgroundColor")


LIGHT_BG, DARK_BG = "rgb(244, 245, 247)", "rgb(15, 17, 21)"


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_theme_toggle_names_its_action_switches_and_remembers(live_server, page, scheme):
    page.emulate_media(color_scheme=scheme)
    page.goto(live_server + "/")
    toggle = page.locator("#theme-toggle")
    other = "dark" if scheme == "light" else "light"
    expect(toggle).to_have_text(f"{other.capitalize()} theme")  # names what a click does

    toggle.click()

    expect(toggle).to_have_text(f"{scheme.capitalize()} theme")
    assert _background(page) == {"light": LIGHT_BG, "dark": DARK_BG}[other]
    assert page.evaluate("localStorage.getItem('theme')") == other
    page.reload()  # the saved choice wins over the system scheme
    assert _background(page) == {"light": LIGHT_BG, "dark": DARK_BG}[other]
    expect(page.locator("#theme-toggle")).to_have_text(f"{scheme.capitalize()} theme")


def test_the_header_names_the_app_with_its_mark(live_server, page):
    page.goto(live_server + "/")

    assert page.title() == "LLM PDF OCR"
    title = page.locator("h1.app-title")
    expect(title).to_have_text("LLM PDF OCR")
    expect(title.locator("svg.logo-mark")).to_be_visible()
    fill = title.locator(".logo-mark-page").evaluate("(p) => getComputedStyle(p).fill")
    assert fill == "rgb(109, 40, 217)"  # the violet accent, #6D28D9


def test_try_a_sample_reads_the_bundled_pdf_without_a_file(live_server, page, fresh_jobs):
    page.goto(live_server + "/")
    page.click("#sample")
    _done(page)

    expect(page.locator("#drop-text")).to_have_text("sample.pdf")
    cards = page.locator(".page-result")
    expect(cards).to_have_count(4)
    expect(cards.nth(0).locator(".method")).to_have_text("Method: Text layer")
    expect(cards.nth(1).locator(".method")).to_contain_text("Method: Tesseract")


def test_the_empty_state_explains_each_method_and_the_vision_line_needs_a_model(live_server, page):
    page.goto(live_server + "/")
    methods = page.locator(".methods dt:visible")
    expect(methods).to_have_text(["Auto", "Text layer", "Tesseract"])
    page.route(
        "**/v1/capabilities",
        lambda route: route.fulfill(
            json={"methods": ["auto", "text_layer", "tesseract", "vision"], "models": ["m"]}
        ),
    )
    page.reload()
    expect(methods).to_have_text(["Auto", "Text layer", "Tesseract", "Vision model"])


def test_a_failure_is_a_banner(live_server, page, fresh_jobs):
    _extract(page, live_server, b"this is not a pdf")

    status = page.locator("#status")
    expect(status).to_have_class(re.compile("error"))
    border = status.evaluate("(s) => getComputedStyle(s).borderLeftWidth")
    assert border == "4px"
    assert status.evaluate("(s) => getComputedStyle(s).backgroundColor") != "rgba(0, 0, 0, 0)"


def test_at_desktop_width_each_page_image_sits_beside_its_text(live_server, page, fresh_jobs):
    page.set_viewport_size({"width": 1440, "height": 900})
    _extract(page, live_server, _pdf(PAGE_ONE))
    _done(page)

    card = page.locator(".page-result")
    image = card.locator(".page-figure").bounding_box()
    body = card.locator(".page-body").bounding_box()
    assert body["x"] >= image["x"] + image["width"]
    assert abs(body["y"] - image["y"]) < 2


def test_each_page_image_is_a_thumbnail_that_opens_full_size(live_server, page, fresh_jobs):
    page.set_viewport_size({"width": 1440, "height": 900})
    _extract(page, live_server, _pdf(PAGE_ONE))
    _done(page)

    card = page.locator(".page-result")
    image = card.locator("img.page-image")
    page.wait_for_function("(i) => i.complete && i.naturalWidth > 0", arg=image.element_handle())
    assert image.bounding_box()["height"] <= 240
    link = card.locator("a.page-link")
    assert link.get_attribute("href") == image.get_attribute("src")
    assert link.get_attribute("target") == "_blank"
    assert "noopener" in link.get_attribute("rel")


@pytest.mark.parametrize(
    ("texts", "line"), [((PAGE_ONE,), "Done: 1 page."), ((PAGE_ONE, PAGE_TWO), "Done: 2 pages.")]
)
def test_the_done_line_counts_pages_in_plain_english(live_server, page, fresh_jobs, texts, line):
    _extract(page, live_server, _pdf(*texts))

    expect(page.locator("#status")).to_have_text(line, timeout=20_000)


def test_the_vision_line_is_plain(live_server, page):
    page.goto(live_server + "/")

    expect(page.locator("#vision-help dd")).to_have_text(
        "An AI vision model reads the page image. The image is sent to that model."
    )


# Find in the result: a plain-text, case-insensitive search over the page texts and the
# Markdown or JSON output, entirely in the browser.


# Expected counts are worked out by hand from the texts, never computed with the same
# lowercase-and-count rule the page uses, so a case-mapping mistake cannot pass both sides.
# Each page text says "Page" once ("paragraph" is not a match). The Markdown output adds
# "## Page 1", "Page one", "## Page 2", "Page two"; the JSON output adds "pages", "page": 1,
# "Page one", "page": 2, "Page two", "page_count" and "failed_pages".
_PAGE_MARKS = {
    "markdown": ["Page", "Page", "Page", "Page", "Page", "Page"],
    "json": ["Page", "Page", "page", "page", "Page", "page", "Page", "page", "page"],
}


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_find_highlights_every_match_in_the_page_texts_and_the_output(
    live_server, page, fresh_jobs, fmt
):
    _result_doc(live_server, page, fmt=fmt)
    output = page.locator("#result").text_content()
    expected = {"markdown": 6, "json": 9}[fmt]

    page.fill("#find-input", "page")

    expect(page.locator("#find-count")).to_have_text(f"1 of {expected}")
    marks = page.locator("mark.find-match")
    expect(marks).to_have_count(expected)
    assert marks.all_text_contents() == _PAGE_MARKS[fmt]
    expect(marks.nth(0)).to_have_class(re.compile(r"\bcurrent\b"))
    expect(page.locator(".page-result").nth(0).locator("mark")).to_have_count(1)
    expect(page.locator("#result mark")).to_have_count(expected - 2)
    # Highlighting never changes the text itself.
    expect(page.locator(".page-result").nth(0).locator(".text")).to_have_text(PAGE_ONE)
    assert page.locator("#result").text_content() == output
    expect(page.locator("#find-prev")).to_be_enabled()
    expect(page.locator("#find-next")).to_be_enabled()


def test_find_steps_with_keys_and_buttons_wrapping_and_scrolling(live_server, page, fresh_jobs):
    texts = [f"Needle number {n} hides on this page." for n in range(1, 7)]
    _extract(page, live_server, _pdf(*texts))
    _done(page)
    total = 12  # one match per page card, one per page in the Markdown output

    box = page.locator("#find-input")
    page.fill("#find-input", "needle")
    expect(page.locator("#find-count")).to_have_text(f"1 of {total}")

    box.press("Enter")
    expect(page.locator("#find-count")).to_have_text(f"2 of {total}")
    current = page.locator("mark.find-match.current")
    expect(current).to_have_count(1)
    expect(current).to_have_text("Needle")
    expect(current).to_be_in_viewport()
    expect(page.locator(".page-result").nth(1).locator("mark.current")).to_have_count(1)

    box.press("Shift+Enter")
    expect(page.locator("#find-count")).to_have_text(f"1 of {total}")
    box.press("Shift+Enter")  # before the first wraps to the last, in the output
    expect(page.locator("#find-count")).to_have_text(f"{total} of {total}")
    expect(page.locator("#result mark.current")).to_have_count(1)
    expect(page.locator("mark.current")).to_be_in_viewport()
    assert page.evaluate("window.scrollY") > 0

    page.click("#find-next")  # past the last wraps to the first
    expect(page.locator("#find-count")).to_have_text(f"1 of {total}")
    page.click("#find-prev")
    expect(page.locator("#find-count")).to_have_text(f"{total} of {total}")
    expect(page.locator("mark.current")).to_have_count(1)


def test_find_enter_right_after_typing_steps_without_waiting_for_the_debounce(
    live_server, page, fresh_jobs
):
    texts = [f"Needle number {n} hides on this page." for n in range(1, 7)]
    _extract(page, live_server, _pdf(*texts))
    _done(page)
    total = 12  # one match per page card, one per page in the Markdown output

    # Count every runFind execution, including the synchronous one stepFind runs when
    # Enter arrives before the debounce; the timer-based pass clock cannot see that one.
    assert page.evaluate("typeof window.runFind") == "function"
    page.evaluate("""() => {
      window.__findRuns = 0;
      const real = window.runFind;
      window.runFind = (...args) => {
        window.__findRuns += 1;
        return real(...args);
      };
    }""")
    # Type the whole query and press Enter in one JS turn, so the 150 ms debounce
    # cannot have fired: the step must search the new query itself, exactly once.
    page.evaluate("""() => {
      const input = document.getElementById("find-input");
      input.focus();
      for (const ch of "needle") {
        input.value += ch;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
      input.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true })
      );
    }""")

    expect(page.locator("#find-count")).to_have_text(f"2 of {total}")
    expect(page.locator("mark.find-match")).to_have_count(total)
    current = page.locator("mark.find-match.current")
    expect(current).to_have_count(1)
    expect(current).to_be_in_viewport()
    expect(page.locator(".page-result").nth(1).locator("mark.current")).to_have_count(1)
    assert page.evaluate("window.__findRuns") == 1
    page.wait_for_timeout(300)  # longer than the debounce: no second pass may arrive
    expect(page.locator("#find-count")).to_have_text(f"2 of {total}")
    assert page.evaluate("window.__findRuns") == 1


def test_find_escape_clears_the_query_and_every_highlight(live_server, page, fresh_jobs):
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
    _done(page)
    page.fill("#find-input", "page")
    expect(page.locator("#find-count")).to_have_text(re.compile(r"^1 of \d+$"))

    page.locator("#find-input").press("Escape")

    expect(page.locator("#find-input")).to_have_value("")
    expect(page.locator("mark.find-match")).to_have_count(0)
    expect(page.locator("#find-count")).to_have_text("")
    expect(page.locator("#find-prev")).to_be_disabled()
    expect(page.locator("#find-next")).to_be_disabled()
    expect(page.locator(".page-result").nth(0).locator(".text")).to_have_text(PAGE_ONE)
    assert page.evaluate("document.activeElement.id") == "find-input"


def test_find_backspacing_to_an_empty_query_clears_and_the_page_stays_alive(
    live_server, page, fresh_jobs
):
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
    _done(page)
    output = page.locator("#result").text_content()
    box = page.locator("#find-input")
    box.fill("page")
    expect(page.locator("#find-count")).to_have_text(re.compile(r"^1 of \d+$"))
    expect(page.locator("mark.find-match").first).to_be_visible()

    for _ in range(len("page")):  # delete the query the way a user does, key by key
        box.press("Backspace")

    expect(page.locator("#find-input")).to_have_value("")
    expect(page.locator("#find-count")).to_have_text("")
    expect(page.locator("mark.find-match")).to_have_count(0)
    expect(page.locator("#find-prev")).to_be_disabled()
    expect(page.locator("#find-next")).to_be_disabled()
    # The texts are restored exactly, and the page still responds and searches again.
    expect(page.locator(".page-result").nth(0).locator(".text")).to_have_text(PAGE_ONE)
    assert page.locator("#result").text_content() == output
    assert page.evaluate("1 + 1") == 2
    box.fill("hello")  # page one's text, and the same text in the Markdown output
    expect(page.locator("#find-count")).to_have_text("1 of 2")
    assert page.locator("mark.find-match").all_text_contents() == ["hello", "hello"]


def test_find_with_no_match_says_so_and_cannot_step(live_server, page, fresh_jobs):
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
    _done(page)

    page.fill("#find-input", "zzz-no-such-word")

    expect(page.locator("#find-count")).to_have_text("No matches")
    expect(page.locator("mark.find-match")).to_have_count(0)
    expect(page.locator("#find-prev")).to_be_disabled()
    expect(page.locator("#find-next")).to_be_disabled()
    page.locator("#find-input").press("Enter")
    expect(page.locator("#find-count")).to_have_text("No matches")

    page.fill("#find-input", "page")  # a new query searches again
    expect(page.locator("#find-count")).to_have_text(re.compile(r"^1 of \d+$"))


def test_find_is_literal_text_not_a_regex(live_server, page, fresh_jobs):
    text = "a.c and abc and a.c; 1.50 not 1x50; [x] not x; (y) not y."
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    _extract(page, live_server, _pdf(text))
    _done(page)

    # The text appears twice: in the page card and in the Markdown output. "abc" and
    # "1x50" would match a regex but not the literal text.
    for query, expected in (("a.c", 4), ("A.C", 4), ("1.50", 2), ("[x]", 2), ("(y)", 2)):
        page.fill("#find-input", query)
        expect(page.locator("#find-count")).to_have_text(f"1 of {expected}")
        marks = page.locator("mark.find-match")
        expect(marks).to_have_count(expected)
        # Consecutive queries can share a count ("1 of 2"), so wait for the new marks
        # themselves, not only the count, before reading them.
        expect(marks.nth(0)).to_have_text(re.compile(rf"^{re.escape(query)}$", re.IGNORECASE))
        assert marks.all_text_contents() == [query.lower()] * expected
    assert errors == []


def _one_page_result(page, text: str) -> None:
    """Answer the first poll with a done job of one text-layer page that says `text`."""
    doc = Document(pages=[Page(number=1, status="ok", method=Method.TEXT_LAYER, text=text)])
    page.route(
        re.compile(r"/pages/\d+\.png$"),
        lambda route: route.fulfill(status=200, content_type="image/png", body=_PNG_1PX),
    )
    _first_poll(
        page,
        {
            "status": "done",
            "pages_total": 1,
            "pages_done": 1,
            "result": json.loads(render_json(doc)),
            "error": None,
        },
    )


def test_find_highlights_the_original_text_when_lowercasing_expands_a_character(
    live_server, page, fresh_jobs
):
    # "İ" (U+0130) lowercases to two code units, "i" and a combining dot, so offsets into
    # the lowercased text run ahead of the original. The text is in the page card and in the
    # Markdown output, so every match below is found twice.
    text = "İİ needle and NEEDLE"
    _one_page_result(page, text)
    _extract(page, live_server, _pdf(PAGE_ONE))
    _done(page)
    marks = page.locator("mark.find-match")
    card = page.locator(".page-result").nth(0).locator(".text")

    page.fill("#find-input", "needle")
    expect(page.locator("#find-count")).to_have_text("1 of 4")
    assert marks.all_text_contents() == ["needle", "NEEDLE", "needle", "NEEDLE"]
    expect(card).to_have_text(text)

    page.fill("#find-input", "İİ")
    expect(page.locator("#find-count")).to_have_text("1 of 2")
    assert marks.all_text_contents() == ["İİ", "İİ"]
    expect(card).to_have_text(text)

    # "i" matches only the first half of each lowercased "İ": the mark takes the whole "İ".
    page.fill("#find-input", "i")
    expect(page.locator("#find-count")).to_have_text("1 of 4")
    assert marks.all_text_contents() == ["İ", "İ", "İ", "İ"]
    expect(card).to_have_text(text)


def test_find_matches_final_sigma_and_never_overlaps_inside_an_expanded_character(
    live_server, page, fresh_jobs
):
    # Each match is found twice: in the page card and in the Markdown output.
    text = "ΟΔΟΣ οδος İİİ"
    _one_page_result(page, text)
    _extract(page, live_server, _pdf(PAGE_ONE))
    _done(page)
    marks = page.locator("mark.find-match")
    card = page.locator(".page-result").nth(0).locator(".text")

    # Lowercased alone, "Σ" is "σ", while a typed word ends in "ς": both words match.
    page.fill("#find-input", "οδος")
    expect(page.locator("#find-count")).to_have_text("1 of 4")
    assert marks.all_text_contents() == ["ΟΔΟΣ", "οδος", "ΟΔΟΣ", "οδος"]
    expect(card).to_have_text(text)

    # A combining dot then "i" matches from inside the first "İ" into the second, so the
    # mark is "İİ". The next try, from the second "İ"'s own dot, would overlap that mark;
    # it is skipped, and the third "İ" alone cannot match.
    page.fill("#find-input", "\u0307i")
    expect(page.locator("#find-count")).to_have_text("1 of 2")
    assert marks.all_text_contents() == ["İİ", "İİ"]
    expect(card).to_have_text(text)


def test_find_never_sends_anything_to_the_server(live_server, page, fresh_jobs):
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
    _done(page)
    page.wait_for_load_state("networkidle")
    seen = []
    page.on("request", lambda request: seen.append(request.url))

    page.fill("#find-input", "page")
    expect(page.locator("#find-count")).to_have_text(re.compile(r"^1 of \d+$"))
    box = page.locator("#find-input")
    box.press("Enter")
    box.press("Shift+Enter")
    page.click("#find-next")
    page.click("#find-prev")
    box.press("Escape")
    page.wait_for_timeout(300)  # longer than the debounce, so a late pass would show

    # Scrolling can load a lazy page thumbnail; that is the result's own image, not find
    # traffic. Nothing else may leave the page.
    assert [url for url in seen if not re.search(r"/pages/\d+\.png$", url)] == []


_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

# Measure the debounced highlight pass itself: wrap setTimeout so only the find box's
# 150 ms timer is timed. A wall-clock bound per keystroke would include the debounce and
# the CI runner's timer jitter; the work the box does per keystroke is what must be fast.
_FIND_PASS_CLOCK = """() => {
  window.__findPasses = [];
  const real = window.setTimeout.bind(window);
  window.setTimeout = (fn, ms, ...rest) => real(() => {
    if (ms !== 150) return fn();
    const start = performance.now();
    fn();
    window.__findPasses.push(performance.now() - start);
  }, ms, ...rest);
}"""


@pytest.mark.parametrize("fmt", ["markdown", "json"])
def test_find_on_500_pages_is_one_pass_per_burst_under_200_ms(live_server, page, fresh_jobs, fmt):
    doc = Document(
        pages=[
            Page(
                number=n,
                status="ok",
                method=Method.TEXT_LAYER,
                text=f"Page {n} of the long document hides a needle in its text.",
            )
            for n in range(1, 501)
        ]
    )
    page.route(
        re.compile(r"/pages/\d+\.png$"),
        lambda route: route.fulfill(status=200, content_type="image/png", body=_PNG_1PX),
    )
    _first_poll(
        page,
        {
            "status": "done",
            "pages_total": 500,
            "pages_done": 500,
            "result": json.loads(render_json(doc)),
            "error": None,
        },
    )
    _extract(page, live_server, _pdf(PAGE_ONE), fmt=fmt)
    _done(page)
    expect(page.locator(".page-result")).to_have_count(500)
    expected = 1000  # one needle in each of the 500 page cards, and one in each page's output

    page.evaluate(_FIND_PASS_CLOCK)
    # A burst of keystrokes with no gap: the debounce must coalesce them into one pass.
    page.evaluate("""() => {
      const input = document.getElementById("find-input");
      input.focus();
      for (const ch of "needle") {
        input.value += ch;
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    }""")
    expect(page.locator("#find-count")).to_have_text(f"1 of {expected}")
    expect(page.locator("mark.find-match")).to_have_count(expected)
    passes = page.evaluate("window.__findPasses")
    assert len(passes) == 1, passes
    assert passes[0] < 200, passes

    box = page.locator("#find-input")
    box.press("s")  # "needles": every highlight must be restored in one more pass
    expect(page.locator("#find-count")).to_have_text("No matches")
    expect(page.locator("mark.find-match")).to_have_count(0)
    box.press("Backspace")
    expect(page.locator("#find-count")).to_have_text(f"1 of {expected}")
    passes = page.evaluate("window.__findPasses")
    assert len(passes) == 3, passes
    assert all(t < 200 for t in passes), passes

    box.press("Enter")  # stepping moves the current mark, it never re-highlights
    expect(page.locator("#find-count")).to_have_text(f"2 of {expected}")
    assert len(page.evaluate("window.__findPasses")) == 3


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_find_box_and_highlights_meet_wcag_aa_in_both_themes(live_server, page, fresh_jobs, scheme):
    page.emulate_media(color_scheme=scheme)
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
    _done(page)
    page.fill("#find-input", "page")
    expect(page.locator("#find-count")).to_have_text(re.compile(r"^1 of \d+$"))

    seen = page.evaluate(_CONTRAST, "select, #find-input")
    whats = {s["what"] for s in seen}
    assert {"find-input", "find-count", "find-prev", "find-next"} <= whats
    assert {"find-match", "find-match current"} <= whats
    low = [s for s in seen if s["ratio"] < 4.5]
    assert low == [], low


@pytest.mark.parametrize("width", [375, 320])
def test_find_bar_fits_and_steps_at_phone_width(live_server, page, fresh_jobs, width):
    page.set_viewport_size({"width": width, "height": 800})
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
    _done(page)
    page.fill("#find-input", "page")
    expect(page.locator("#find-count")).to_have_text(re.compile(r"^1 of \d+$"))

    widths = page.evaluate(_WIDTHS)
    assert widths[0] <= widths[1] == width
    for selector in ("#find-input", "#find-count", "#find-prev", "#find-next"):
        box = page.locator(selector).bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= width, selector
    page.locator("#find-input").press("Enter")
    expect(page.locator("#find-count")).to_have_text(re.compile(r"^2 of \d+$"))


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_find_controls_are_keyboard_reachable_with_a_focus_ring(
    live_server, page, fresh_jobs, scheme
):
    page.emulate_media(color_scheme=scheme)
    _extract(page, live_server, _pdf(PAGE_ONE, PAGE_TWO))
    _done(page)
    page.fill("#find-input", "page")  # an active query enables the step buttons
    expect(page.locator("#find-count")).to_have_text(re.compile(r"^1 of \d+$"))

    for element_id in ("find-input", "find-prev", "find-next"):
        _tab_to(page, element_id)
        ring = page.evaluate(_RING)
        assert ring["id"] == element_id
        assert ring["style"] not in ("none", "auto") and ring["width"] >= 2, ring
