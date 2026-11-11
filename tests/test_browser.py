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
    expect(cards.nth(0).locator(".method")).to_have_text("Method: vision · Model: m-one")
    expect(cards.nth(0).locator(".fallback")).to_have_count(0)
    expect(cards.nth(1).locator(".method")).to_have_text("Method: tesseract · Confidence: 0.87")
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
    assert set(ids) == CONTROLS | {"model"}
    names = {
        "theme-toggle": "Light theme" if page.evaluate(_DARK) else "Dark theme",
        "file": "PDF file",
        "sample": "Try a sample",
        "format": "Output",
        "method": "Method",
        "model": "Model",
        "submit": "Extract",
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
    expect(cards.nth(0).locator(".method")).to_contain_text("text_layer")
    expect(cards.nth(1).locator(".method")).to_contain_text("tesseract")


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
