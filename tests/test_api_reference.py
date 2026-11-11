"""The API reference at `/docs`: Scalar, vendored, offline, reading this service's own spec.

The browser test at the bottom loads the page with every request to another origin
blocked, and runs "try it" on `/v1/capabilities`.
"""

import hashlib
import json
import re
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

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
    (version,) = re.findall(r"@scalar/api-reference@(\d+\.\d+\.\d+)", notes)
    (digest,) = re.findall(r"\b([0-9a-f]{64})\b", notes)

    assert version in notes
    assert hashlib.sha256(SCALAR.read_bytes()).hexdigest() == digest


def test_the_reference_loads_nothing_from_another_origin():
    html = client.get("/docs").text
    assets = _assets(html)

    assert assets
    for asset in assets:
        assert asset.startswith("/"), asset
        assert client.get(asset).status_code == 200, asset
    for path, body in {"/docs": html} | {a: client.get(a).text for a in assets}.items():
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


def test_the_page_has_no_inline_script_or_style_the_policy_would_block():
    html = client.get("/docs").text

    for tag in re.findall(r"<script[^>]*>", html):
        assert "src=" in tag or 'id="api-reference"' in tag, tag
    assert re.search(r'<script id="api-reference"[^>]*>\s*</script>', html)
    assert "<style" not in html
    assert not re.search(r"\son\w+=", html)
    assert " style=" not in html


def test_the_policy_allows_this_origin_and_one_nonce_per_request():
    first, second = client.get("/docs"), client.get("/docs")
    policy = first.headers["content-security-policy"]

    assert policy.startswith("default-src 'self'")
    assert "http://" not in policy and "https://" not in policy
    assert "'unsafe-eval'" not in policy
    (nonce,) = re.findall(r"'nonce-([^']+)'", policy)
    assert f'<meta property="csp-nonce" content="{nonce}">' in first.text
    assert nonce not in second.headers["content-security-policy"]


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


# In a real browser, with the network off


@pytest.fixture(scope="module")
def live_server():
    """The app on a free local port, for the test that needs a real browser."""
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
def offline_page(live_server):
    """A browser page where every request to another origin is blocked and recorded."""
    blocked = []

    def route_handler(route):
        if route.request.url.startswith(live_server):
            route.continue_()
        else:
            blocked.append(route.request.url)
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route("**/*", route_handler)
        yield page, blocked
        browser.close()


def test_the_reference_lists_every_endpoint_and_try_it_works_offline(offline_page, live_server):
    page, blocked = offline_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_server + "/docs")

    for path in json.loads((FIXTURES / "openapi_paths.json").read_text()):
        expect(page.get_by_text(path, exact=False).first).to_be_visible(timeout=15000)

    page.get_by_text("/v1/capabilities", exact=False).first.click()
    page.get_by_role("button", name="Test Request").click()
    page.get_by_role("button", name="Send").click()

    expect(page.get_by_text("200", exact=True).first).to_be_visible(timeout=15000)
    expect(page.get_by_text("text_layer").first).to_be_visible(timeout=15000)
    assert blocked == []
    assert errors == []
