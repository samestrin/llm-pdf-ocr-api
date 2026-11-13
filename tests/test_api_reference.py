"""The API reference at `/docs`: Scalar, vendored, offline, reading this service's own spec.

The browser tests at the bottom load the page with every request to another origin
blocked, and run "try it" on `/v1/capabilities`.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright
from test_browser import live_server  # noqa: F401  (a session-scoped fixture)

from llm_pdf_ocr.app import app

client = TestClient(app)

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "llm_pdf_ocr" / "web"
SCALAR = WEB / "scalar" / "standalone.js"
SCRIPT = "/static/scalar/standalone.js"
FIXTURES = Path(__file__).parent / "fixtures"


def _assets(html: str) -> list[str]:
    return re.findall(r'(?:src|href)="([^"]+)"', html)


def _configuration(html: str) -> dict:
    """The JSON in the reference's `data-configuration` attribute."""
    (attribute,) = re.findall(r'data-configuration="([^"]*)"', html)
    return json.loads(attribute.replace("&quot;", '"').replace("&amp;", "&"))


def test_docs_is_html_that_points_at_the_spec_and_the_vendored_script():
    response = client.get("/docs")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/openapi.json" in response.text
    assert SCRIPT in _assets(response.text)


def test_docs_and_redoc_are_not_in_the_schema():
    paths = client.get("/openapi.json").json()["paths"]

    assert "/docs" not in paths
    assert "/redoc" not in paths


def test_the_docs_name_the_reference_and_the_raw_spec():
    for name in ("README.md", "docs/api.md"):
        text = (ROOT / name).read_text()
        assert "`/docs`" in text and "Scalar" in text, name
        assert "`/openapi.json`" in text, name


def test_fastapis_default_docs_helper_route_is_gone():
    # FastAPI's default docs page brings an OAuth2 redirect page with it; Scalar does not.
    response = client.get("/docs/oauth2-redirect")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_redoc_is_gone():
    response = client.get("/redoc")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_openapi_paths_are_unchanged():
    """Snapshot of `paths` taken before the viewer changed; the spec is not this task's to edit."""
    snapshot = json.loads((FIXTURES / "openapi_paths.json").read_text())

    assert client.get("/openapi.json").json()["paths"] == snapshot


def test_the_vendored_script_is_served_by_this_service():
    response = client.get(SCRIPT)

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.content == SCALAR.read_bytes()


def test_the_vendored_script_is_the_pinned_release():
    """The file is the published bundle of the version named beside it, byte for byte."""
    notes = (WEB / "scalar" / "README.md").read_text()
    versions = set(re.findall(r"@scalar/api-reference@(\d+\.\d+\.\d+)", notes))
    (digest,) = re.findall(r"\b([0-9a-f]{64})\b", notes)

    assert len(versions) == 1
    assert hashlib.sha256(SCALAR.read_bytes()).hexdigest() == digest


def test_the_reference_loads_nothing_from_another_origin():
    html = client.get("/docs").text
    assets = _assets(html)

    assert assets
    for asset in assets:
        assert asset.startswith("/"), asset
        assert client.get(asset).status_code == 200, asset
    # The vendored bundle is a third-party file full of documentation links; what it
    # requests at run time is measured in the browser test at the bottom instead.
    ours = {"/docs": html} | {
        a: client.get(a).text for a in assets if not a.startswith("/static/scalar/")
    }
    for path, body in ours.items():
        # The SVG namespace in the favicon is a name, not a request to that origin.
        body = body.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert not re.search(r"https?:", body, re.IGNORECASE), path
        assert not re.search(r"""(?:src|href|action)\s*=\s*["']?//""", body), path


def test_the_configuration_reads_this_spec_and_turns_off_every_scalar_service():
    config = _configuration(client.get("/docs").text)

    assert config["url"] == "/openapi.json"
    assert config["proxyUrl"] is None  # no request goes through proxy.scalar.com
    assert config["withDefaultFonts"] is False  # no font is fetched
    assert config["telemetry"] is False
    assert config["hideClientButton"] is True  # no button opens client.scalar.com


def test_the_reference_carries_the_apps_favicon():
    html = client.get("/docs").text

    assert 'rel="icon" type="image/svg+xml" href="/static/favicon.svg"' in html


def test_the_accent_is_the_apps_violet_in_both_themes():
    css = client.get("/static/docs.css").text

    assert "--scalar-color-accent" in css
    assert "#6d28d9" in css  # light, as in web/style.css
    assert "#c4b5fd" in css  # dark


def test_the_page_ships_no_inline_script_or_style():
    html = client.get("/docs").text

    for tag in re.findall(r"<script[^>]*>", html):
        assert "src=" in tag or 'id="api-reference"' in tag, tag
    assert re.search(r'<script id="api-reference"[^>]*>\s*</script>', html)
    assert "<style" not in html
    assert not re.search(r"\son\w+=", html)
    assert " style=" not in html


def _directives(policy: str) -> dict[str, str]:
    return dict(part.strip().split(" ", 1) for part in policy.split(";") if part.strip())


def test_the_policy_keeps_every_other_origin_out():
    directives = _directives(client.get("/docs").headers["content-security-policy"])

    assert directives["default-src"] == "'self'"
    # Scalar injects its own stylesheets while it draws the page; scripts stay strict.
    assert directives["style-src"] == "'self' 'unsafe-inline'"
    for value in directives.values():
        assert "http" not in value
        assert "'unsafe-eval'" not in value
    assert "unsafe-inline" not in directives.get("script-src", directives["default-src"])


def test_docs_is_read_only():
    assert client.post("/docs").status_code == 405


@pytest.fixture
def page_off():
    app.state.web_page = False
    yield
    app.state.web_page = True


def test_docs_is_404_when_the_page_is_off(page_off):
    response = client.get("/docs")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "content-security-policy" not in response.headers


def test_the_vendored_script_is_404_when_the_page_is_off(page_off):
    assert client.get(SCRIPT).status_code == 404


def test_the_api_still_answers_when_the_page_is_off(page_off):
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/v1/capabilities").status_code == 200


# In a real browser, with the network off. The server is the one `test_browser.py`
# starts for the whole session: a second one would run the app's lifespan again, and
# its shutdown stops the job store every later browser test needs.


@pytest.fixture
def offline_page(live_server):  # noqa: F811
    """A browser page where every request to another origin is blocked and recorded."""
    blocked = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        _block_other_origins(page, live_server, blocked)
        yield page, blocked
        browser.close()


def _block_other_origins(page, base: str, blocked: list) -> None:
    def handler(route):
        if route.request.url.startswith(base):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    page.route("**/*", handler)


def _operations() -> set[str]:
    """Every operation in the snapshot, as the reference's sidebar links to it."""
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    snapshot = json.loads((FIXTURES / "openapi_paths.json").read_text())
    return {
        f"#{method.upper()}{path}"
        for path, item in snapshot.items()
        for method in item
        if method in methods
    }


def test_the_reference_lists_every_endpoint_and_try_it_works_offline(offline_page, live_server):  # noqa: F811
    page, blocked = offline_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_server + "/docs")

    expect(page.get_by_role("link", name="Capabilities")).to_be_visible(timeout=20000)
    hrefs = page.eval_on_selector_all("a", "links => links.map(a => a.getAttribute('href'))")
    assert _operations() <= set(hrefs)

    page.get_by_role("button", name=re.compile(r"Test Request.*capabilities")).click()
    dialog = page.get_by_role("dialog", name="API Client")
    dialog.get_by_role("button", name=re.compile(r"Send get request to .*capabilities")).click()

    expect(dialog.get_by_text("200 OK")).to_be_visible(timeout=15000)
    expect(dialog.get_by_text("text_layer")).to_be_visible(timeout=15000)
    # Nothing left this service, on load or while running the request: no proxy, no
    # font, no telemetry.
    assert blocked == []
    # Scalar probes for eval once and falls back when the policy refuses it; that is a
    # console notice, not a page error, and the policy must not be widened for it.
    assert errors == []


@pytest.mark.parametrize(("scheme", "accent"), [("light", "#6d28d9"), ("dark", "#c4b5fd")])
def test_the_reference_is_violet_in_both_themes_in_a_browser(live_server, scheme, accent):  # noqa: F811
    blocked = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(color_scheme=scheme)
        _block_other_origins(page, live_server, blocked)
        page.goto(live_server + "/docs")
        expect(page.get_by_role("link", name="Capabilities")).to_be_visible(timeout=20000)

        assert page.evaluate("document.body.className") == f"{scheme}-mode"
        assert (
            page.evaluate(
                "getComputedStyle(document.body).getPropertyValue('--scalar-color-accent').trim()"
            )
            == accent
        )
        assert blocked == []
        browser.close()
