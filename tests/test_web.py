import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_pdf_ocr import __version__
from llm_pdf_ocr.app import _duration, app, get_public_demo_config, get_vision_config
from llm_pdf_ocr.config import PublicDemoConfig, VisionConfig, load_limits

client = TestClient(app)

WEB = Path(__file__).resolve().parents[1] / "src" / "llm_pdf_ocr" / "web"

# The one external URL the page may reference: the footer's licence link (EST-9382).
LICENSE_URL = "https://github.com/samestrin/llm-pdf-ocr-api/blob/main/LICENSE"


def _assets(html: str) -> list[str]:
    # A fragment such as the skip link's "#main-content" is a place on the page, not a file.
    return re.findall(r'(?:src|href)="([^"#][^"]*)"', html)


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
    # The licence link is a navigation link to GitHub, not an asset this service serves.
    assets = [a for a in _assets(client.get("/").text) if a != LICENSE_URL]

    assert any(a.endswith(".js") for a in assets)
    assert any(a.endswith(".css") for a in assets)
    for asset in assets:
        assert asset.startswith("/"), asset
        assert client.get(asset).status_code == 200, asset


def _references_external_origin(body: str) -> bool:
    # The SVG namespace in the favicon is a name, not a request to that origin.
    body = body.replace('xmlns="http://www.w3.org/2000/svg"', "")
    # The footer's licence link is the one allowed external URL, and only as a
    # complete quoted value; a near-miss of it is still refused (EST-9382).
    body = body.replace(f'"{LICENSE_URL}"', '""')
    return bool(
        re.search(r"https?:", body, re.IGNORECASE)
        or re.search(r"""(?:src|href|action)\s*=\s*["']?//""", body)
        or re.search(r"""url\(\s*["']?//""", body)
        or re.search(r"""@import""", body)
        or re.search(r"""(?:fetch|import)\(\s*["'`]//""", body)
    )


def test_page_and_assets_reference_no_external_origin():
    html = client.get("/").text
    assets = [a for a in _assets(html) if a != LICENSE_URL]
    bodies = {"/": html} | {a: client.get(a).text for a in assets}

    for path, body in bodies.items():
        assert not _references_external_origin(body), path


def test_external_origin_check_accepts_the_licence_url():
    assert not _references_external_origin(f'<a href="{LICENSE_URL}">MIT License</a>')


def test_external_origin_check_rejects_any_other_external_url():
    assert _references_external_origin('<a href="https://evil.example/LICENSE">MIT License</a>')
    assert _references_external_origin(f'<a href="{LICENSE_URL}-other">MIT License</a>')


def test_page_has_no_inline_script_or_style_the_policy_would_block():
    html = client.get("/").text

    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert "<style" not in html
    assert not re.search(r"\son\w+=", html)
    assert " style=" not in html


def _footer(html: str) -> str:
    footer = re.search(r'<footer class="app-footer">(.*?)</footer>', html, re.S)
    assert footer, "the page has no footer"
    return footer[1]


def test_page_footer_states_the_copyright_and_links_the_mit_license():
    footer = _footer(client.get("/").text)

    # The year is filled in by app.py like {{version}}. T6 (EST-9382) supersedes T3's
    # plain-text licence: it links to the repo's LICENSE on GitHub, the one allowed
    # external URL.
    assert f"© 2024–{date.today().year} Sam Estrin · " in footer
    copy = re.search(r'<p class="footer-copy">(.*?)</p>', footer, re.S)
    assert copy, "the footer has no copyright line"
    assert f'<a href="{LICENSE_URL}">MIT License</a>' in copy[1]
    assert "{{" not in footer


def test_page_footer_mit_license_href_is_exactly_the_repo_licence_url():
    footer = _footer(client.get("/").text)

    assert re.findall(r'<a href="([^"]*)">MIT License</a>', footer) == [LICENSE_URL]


def test_page_footer_links_row_links_only_pages_the_app_serves():
    footer = _footer(client.get("/").text)

    # The app serves no Privacy, Terms, About or Guide page, so the links row is the
    # API reference alone, with the version last as text, not a link.
    assert '<nav aria-label="Footer">' in footer
    items = re.findall(r"<li>(.*?)</li>", footer, re.S)
    assert items == ['<a href="/docs">API</a>', f"v{__version__}"]
    assert client.get("/docs").status_code == 200


def test_the_docs_page_has_no_app_footer():
    assert "app-footer" not in client.get("/docs").text


def test_page_files_ship_inside_the_package():
    names = {p.name for p in WEB.iterdir()}

    assert {"index.html", "app.js", "style.css", "theme.js", "favicon.svg"} <= names


def test_missing_asset_is_404():
    assert client.get("/static/nope.js").status_code == 404


# Every preset by id, label and vision model: never a base URL.
PRESETS = [
    {"id": "openai", "label": "OpenAI", "vision_model": "gpt-5-nano"},
    {"id": "openrouter", "label": "OpenRouter", "vision_model": "qwen/qwen3.7-flash"},
    {"id": "synthetic", "label": "Synthetic", "vision_model": "syn:small:vision"},
    {"id": "alibaba", "label": "Alibaba (DashScope intl)", "vision_model": "qwen-vl-plus"},
    {"id": "demo", "label": "Demo code", "vision_model": "demo-vision"},
]


def test_capabilities_without_vision():
    response = client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "methods": ["auto", "text_layer", "tesseract"],
        "models": [],
        "presets": PRESETS,
        "demo_available": False,
        "notice": None,
    }
    assert '"vision"' not in response.text  # as the Docker job in CI checks


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
        "presets": PRESETS,
        "demo_available": False,
        "notice": None,
    }
    assert secret_key not in response.text
    assert "llm.internal.example" not in response.text
    assert "http" not in response.text


def test_capabilities_with_a_demo_gateway_and_notice_show_neither_url():
    gateway = "http://gateway.internal:4000/v1"
    notice = "Public demo: do not upload private files. Uploads are deleted after 10 minutes."
    app.dependency_overrides[get_public_demo_config] = lambda: PublicDemoConfig(
        gateway_url=gateway, notice=notice
    )

    response = client.get("/v1/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "methods": ["auto", "text_layer", "tesseract"],
        "models": [],
        "presets": PRESETS,
        "demo_available": True,
        "notice": notice,
    }
    assert "gateway.internal" not in response.text
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
    assert re.search(r'<a class="app-brand" href="/">\s*<svg class="logo-mark"', header)
    assert re.search(r"</svg>\s*PDF OCR\s*</a>", header)
    assert "<h1" not in header
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
    assert "until the job expires, 30 minutes after it ends." in html
    assert "deleted 30 minutes after the job ends." in html
    assert "{{" not in html
    assert "unless this service sets other limits" not in html


def test_the_page_states_the_default_limits_and_version():
    html = client.get("/").text

    assert "up to 50 MB and 500 pages." in html
    assert "until the job expires, 1 hour after it ends." in html
    assert "deleted 1 hour after the job ends." in html
    assert f"<li>v{__version__}</li>" in html


def _privacy_line(html: str) -> str:
    match = re.search(r'<p class="privacy">(.*?)</p>', html, re.S)
    assert match, "the page has no privacy line"
    return " ".join(match.group(1).split())


def test_the_privacy_line_names_the_vision_exception():
    # A job with a page marked needs_vision keeps its PDF until it expires (docs/api.md,
    # Jobs); the page must not tell a visitor the file is always dropped when the job ends.
    line = _privacy_line(client.get("/").text)

    assert "dropped from memory when its job ends, unless a page needs a vision model" in line
    assert "kept in memory until the job expires, 1 hour after it ends" in line


def test_the_privacy_line_agrees_with_the_api_reference_and_web_page_docs():
    docs = Path(__file__).resolve().parents[1] / "docs"
    api = " ".join((docs / "api.md").read_text(encoding="utf-8").split())
    web_page = " ".join((docs / "web-page.md").read_text(encoding="utf-8").split())
    line = _privacy_line((WEB / "index.html").read_text(encoding="utf-8"))

    assert "The PDF is dropped when its job ends, unless a page of its result is marked" in api
    assert "keeps the PDF in memory until it expires" in api
    for text in (line, web_page):
        assert "unless a page needs a vision model" in text
        assert "kept in memory until the job expires" in text


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
