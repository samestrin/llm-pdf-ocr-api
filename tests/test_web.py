import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_pdf_ocr import __version__
from llm_pdf_ocr.app import _duration, app, get_vision_config
from llm_pdf_ocr.config import VisionConfig, load_limits

client = TestClient(app)

WEB = Path(__file__).resolve().parents[1] / "src" / "llm_pdf_ocr" / "web"


def _assets(html: str) -> list[str]:
    return re.findall(r'(?:src|href)="([^"]+)"', html)


def test_page_is_served_at_root():
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<form" in response.text


def test_page_carries_a_self_only_content_security_policy():
    assert client.get("/").headers["content-security-policy"] == "default-src 'self'"


def test_page_offers_both_formats_and_the_methods():
    html = client.get("/").text

    for value in ("markdown", "json", "auto", "text_layer", "tesseract", "vision"):
        assert f'value="{value}"' in html
    assert 'type="file"' in html and 'accept="application/pdf' in html


def test_page_assets_are_served_from_this_service():
    assets = _assets(client.get("/").text)

    assert any(a.endswith(".js") for a in assets)
    assert any(a.endswith(".css") for a in assets)
    for asset in assets:
        assert asset.startswith("/"), asset
        assert client.get(asset).status_code == 200, asset


def test_page_and_assets_reference_no_external_origin():
    html = client.get("/").text
    bodies = {"/": html} | {a: client.get(a).text for a in _assets(html)}

    for path, body in bodies.items():
        # The SVG namespace in the favicon is a name, not a request to that origin.
        body = body.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert not re.search(r"https?:", body, re.IGNORECASE), path
        assert not re.search(r"""(?:src|href|action)\s*=\s*["']?//""", body), path
        assert not re.search(r"""url\(\s*["']?//""", body), path
        assert not re.search(r"""@import""", body), path
        assert not re.search(r"""(?:fetch|import)\(\s*["'`]//""", body), path


def test_page_has_no_inline_script_or_style_the_policy_would_block():
    html = client.get("/").text

    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "<style" not in html
    assert not re.search(r"\son\w+=", html)
    assert " style=" not in html


def test_page_footer_links_to_the_api_reference():
    html = client.get("/").text

    footer = re.search(r'<footer class="app-footer">(.*?)</footer>', html, re.S)
    assert footer, "the page has no footer"
    assert '<a href="/docs">API reference</a>' in footer[1]
    assert client.get("/docs").status_code == 200


def test_page_files_ship_inside_the_package():
    names = {p.name for p in WEB.iterdir()}

    assert {"index.html", "app.js", "style.css", "theme.js", "favicon.svg"} <= names


def test_missing_asset_is_404():
    assert client.get("/static/nope.js").status_code == 404


def test_capabilities_without_vision():
    response = client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {"methods": ["auto", "text_layer", "tesseract"], "models": []}


def test_capabilities_with_vision_lists_only_method_and_model_names():
    secret_key = "sk-very-secret-key"
    endpoint = "https://llm.internal.example:8443/v1"
    app.dependency_overrides[get_vision_config] = lambda: VisionConfig(
        base_url=endpoint,
        api_key=secret_key,
        default_model="qwen2.5vl",
        allowed_models=("qwen2.5vl", "llava"),
    )

    response = client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "methods": ["auto", "text_layer", "tesseract", "vision"],
        "models": ["qwen2.5vl", "llava"],
    }
    assert secret_key not in response.text
    assert "llm.internal.example" not in response.text
    assert "http" not in response.text


def test_capabilities_is_read_only():
    assert client.post("/v1/capabilities").status_code == 405


@pytest.fixture
def page_off():
    app.state.web_page = False
    yield
    app.state.web_page = True


def test_root_is_404_when_the_page_is_off(page_off):
    response = client.get("/")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "content-security-policy" not in response.headers


def test_page_assets_are_404_when_the_page_is_off(page_off):
    assert client.get("/static/app.js").status_code == 404


def test_api_still_answers_when_the_page_is_off(page_off):
    assert client.get("/health").status_code == 200
    assert client.get("/v1/capabilities").status_code == 200


def test_page_script_never_writes_html():
    script = (WEB / "app.js").read_text()

    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in script, sink


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_page_title_header_mark_and_theme_toggle():
    html = client.get("/").text

    assert "<title>LLM PDF OCR</title>" in html
    header = re.search(r'<header class="app-bar">(.*?)</header>', html, re.S)[1]
    assert re.search(r'<h1 class="app-title">\s*<svg class="logo-mark"', header)
    assert "LLM PDF OCR" in header
    assert re.search(r'<button id="theme-toggle"[^>]*>Dark theme</button>', header)
    assert 'rel="icon" type="image/svg+xml" href="/static/favicon.svg"' in html


def test_favicon_has_light_and_dark_colours():
    response = client.get("/static/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "#6D28D9" in response.text
    assert "@media (prefers-color-scheme: dark)" in response.text and "#C4B5FD" in response.text


def test_the_sample_pdf_is_served_and_is_the_mixed_fixture():
    response = client.get("/static/samples/sample.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == (FIXTURES / "mixed.pdf").read_bytes()
    assert len(response.content) < 1024 * 1024


def test_the_sample_is_404_when_the_page_is_off(page_off):
    assert client.get("/static/samples/sample.pdf").status_code == 404


@pytest.fixture
def limits():
    saved_limits, saved_ttl = app.state.limits, app.state.jobs.ttl
    yield
    app.state.limits, app.state.jobs.ttl = saved_limits, saved_ttl


def test_the_page_states_the_configured_limits_and_keep_time(limits):
    app.state.limits = load_limits({"OCR_MAX_FILE_MB": "12.5", "OCR_MAX_PAGES": "40"})
    app.state.jobs.ttl = 1800

    html = client.get("/").text

    assert "up to 12.5 MB and 40 pages." in html
    assert "deleted 30 minutes later." in html
    assert "{{" not in html
    assert "unless this service sets other limits" not in html


def test_the_page_states_the_default_limits_and_version():
    html = client.get("/").text

    assert "up to 50 MB and 500 pages." in html
    assert "deleted 1 hour later." in html
    assert f"LLM PDF OCR {__version__}." in html


@pytest.mark.parametrize(
    ("seconds", "words"),
    [
        (3600, "1 hour"),
        (7200, "2 hours"),
        (90, "90 seconds"),
        (120, "2 minutes"),
        (1.5, "1.5 seconds"),
    ],
)
def test_keep_time_in_words(seconds, words):
    assert _duration(seconds) == words
