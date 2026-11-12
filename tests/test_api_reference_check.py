"""The reference check, proven both ways.

The real app passes today. A fixture app moved behind a path prefix fails
without a `servers` entry -- even when the page and the spec still load -- and
passes with one. The checker is `api_reference_check.py`, the same file CI's
`try-it` job runs against the compose stack.
"""

import json
from html import escape as escape_html

import pytest
from api_reference_check import GET_PATHS, ReferenceCheckError, check_api_reference, main
from api_reference_check import Response as FetchResponse
from fastapi import FastAPI, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.testclient import TestClient
from test_browser import live_server  # noqa: F401  (a session-scoped fixture)

from llm_pdf_ocr.app import app

PREFIX = "/api"


def fetch_with(client: TestClient):
    """The checker's fetch, over a TestClient instead of the network."""

    def fetch(url: str) -> FetchResponse:
        response = client.get(url)
        return FetchResponse(
            response.status_code, response.headers.get("content-type", ""), response.text
        )

    return fetch


def reference_app(
    *,
    servers: list | None = None,
    spec_href: str = "/openapi.json",
    asset_href: str = "/static/reference.js",
    proxy_url: str | None = None,
    health_answer: object = None,
    capabilities_answer: object = None,
) -> FastAPI:
    """A stand-in for the real app: a Scalar-style page, a spec, the two GET routes.

    The hrefs default to absolute paths, like the real page; pass relative ones
    to model a page that keeps loading behind a prefix.
    """
    fixture = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "fixture", "version": "0.0.0"},
        "paths": {
            path: {"get": {"responses": {"200": {"description": "ok"}}}} for path in GET_PATHS
        },
    }
    if servers is not None:
        spec["servers"] = servers
    configuration = {
        "url": spec_href,
        "proxyUrl": proxy_url,
        "withDefaultFonts": False,
        "telemetry": False,
    }
    page = (
        "<!doctype html><html><head>"
        f'<link rel="stylesheet" href="{asset_href}">'
        "</head><body>"
        '<script id="api-reference" data-configuration="'
        f'{escape_html(json.dumps(configuration), quote=True)}"></script>'
        f'<script src="{asset_href}"></script>'
        "</body></html>"
    )

    @fixture.get("/docs", include_in_schema=False)
    def docs() -> HTMLResponse:
        return HTMLResponse(page)

    @fixture.get("/openapi.json", include_in_schema=False)
    def openapi() -> dict:
        return spec

    @fixture.get("/static/reference.js", include_in_schema=False)
    def asset() -> Response:
        return Response("/* fixture */", media_type="application/javascript")

    @fixture.get("/health")
    def health() -> object:
        return {"status": "ok"} if health_answer is None else health_answer

    @fixture.get("/v1/capabilities")
    def capabilities() -> object:
        if capabilities_answer is not None:
            return capabilities_answer
        return {"methods": ["auto", "text_layer", "tesseract"], "models": []}

    return fixture


class PrefixProxy:
    """A reverse proxy that strips the prefix and forwards, without setting
    root_path: the app never learns it sits behind a prefix. Paths outside the
    prefix are the proxy's own 404. This is the failure mode the check guards.
    """

    def __init__(self, app, prefix: str = PREFIX):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope["path"]
        if path == self.prefix or path.startswith(self.prefix + "/"):
            await self.app({**scope, "path": path[len(self.prefix) :] or "/"}, receive, send)
            return
        await JSONResponse({"error": {"code": "not_found"}}, status_code=404)(scope, receive, send)


def test_the_spec_declares_no_servers_today():
    assert "servers" not in TestClient(app).get("/openapi.json").json()


def test_the_real_app_passes_today():
    checked = check_api_reference("http://testserver", fetch_with(TestClient(app)))

    for url in (
        "http://testserver/docs",
        "http://testserver/openapi.json",
        "http://testserver/v1/capabilities",
        "http://testserver/health",
    ):
        assert url in checked


def test_the_fixture_app_passes_at_the_root():
    check_api_reference("http://testserver", fetch_with(TestClient(reference_app())))


def test_the_real_app_behind_a_prefix_without_servers_fails():
    proxy = PrefixProxy(app)

    with pytest.raises(ReferenceCheckError):
        check_api_reference("http://testserver" + PREFIX, fetch_with(TestClient(proxy)))


def test_a_reachable_spec_without_servers_still_fails_behind_a_prefix():
    """The page and the spec load behind the prefix, but "try it" goes to the root."""
    fixture = reference_app(spec_href="openapi.json", asset_href="static/reference.js")
    proxy = PrefixProxy(fixture)

    with pytest.raises(ReferenceCheckError, match=r"http://testserver/v1/capabilities: HTTP 404"):
        check_api_reference("http://testserver" + PREFIX, fetch_with(TestClient(proxy)))


@pytest.mark.parametrize(
    "servers",
    [[{"url": PREFIX}], [{"url": "http://testserver" + PREFIX}]],
    ids=["path", "absolute"],
)
def test_a_prefix_with_a_servers_entry_passes(servers):
    fixture = reference_app(
        servers=servers, spec_href="openapi.json", asset_href="static/reference.js"
    )
    proxy = PrefixProxy(fixture)

    checked = check_api_reference("http://testserver" + PREFIX, fetch_with(TestClient(proxy)))

    assert "http://testserver/api/v1/capabilities" in checked
    assert "http://testserver/api/health" in checked


def test_every_declared_server_is_checked():
    fixture = reference_app(
        servers=[{"url": PREFIX}, {"url": "/elsewhere"}],
        spec_href="openapi.json",
        asset_href="static/reference.js",
    )
    proxy = PrefixProxy(fixture)

    with pytest.raises(ReferenceCheckError, match=r"http://testserver/elsewhere/v1/capabilities"):
        check_api_reference("http://testserver" + PREFIX, fetch_with(TestClient(proxy)))


def test_a_cross_origin_resource_fails():
    fixture = reference_app(asset_href="https://cdn.example.com/reference.js")

    with pytest.raises(ReferenceCheckError, match="leaves the origin"):
        check_api_reference("http://testserver", fetch_with(TestClient(fixture)))


def test_a_cross_origin_spec_fails():
    fixture = reference_app(spec_href="https://elsewhere.example/openapi.json")

    with pytest.raises(ReferenceCheckError, match="leaves the origin"):
        check_api_reference("http://testserver", fetch_with(TestClient(fixture)))


def test_a_request_proxy_fails():
    fixture = reference_app(proxy_url="https://proxy.scalar.com")

    with pytest.raises(ReferenceCheckError, match="proxyUrl"):
        check_api_reference("http://testserver", fetch_with(TestClient(fixture)))


def test_a_missing_reference_page_fails():
    with pytest.raises(ReferenceCheckError, match=r"http://testserver/absent/docs: HTTP 404"):
        check_api_reference("http://testserver/absent", fetch_with(TestClient(reference_app())))


def bare_app() -> FastAPI:
    return FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


SCRIPT = '<script src="/static/reference.js"></script>'
CONFIG = (
    '<script id="api-reference" data-configuration="{&quot;url&quot;: '
    '&quot;/openapi.json&quot;}"></script>'
)


def page_app(html: str, spec_body: str = "{}", spec_type: str = "application/json") -> FastAPI:
    """A reference page with the given HTML, and a spec route with the given answer."""
    fixture = bare_app()

    @fixture.get("/docs")
    def docs() -> HTMLResponse:
        return HTMLResponse(html)

    @fixture.get("/static/reference.js")
    def asset() -> Response:
        return Response("/* fixture */", media_type="application/javascript")

    @fixture.get("/openapi.json")
    def openapi() -> Response:
        return Response(spec_body, media_type=spec_type)

    return fixture


def test_a_page_that_is_not_html_fails():
    fixture = bare_app()

    @fixture.get("/docs")
    def docs() -> dict:
        return {"not": "html"}

    with pytest.raises(ReferenceCheckError, match="not HTML"):
        check_api_reference("http://testserver", fetch_with(TestClient(fixture)))


def test_a_page_without_a_configuration_fails():
    fixture = bare_app()

    @fixture.get("/docs")
    def docs() -> HTMLResponse:
        return HTMLResponse('<html><script src="/static/reference.js"></script></html>')

    @fixture.get("/static/reference.js")
    def asset() -> Response:
        return Response("/* fixture */", media_type="application/javascript")

    with pytest.raises(ReferenceCheckError, match="data-configuration"):
        check_api_reference("http://testserver", fetch_with(TestClient(fixture)))


def test_a_spec_that_is_not_json_fails():
    fixture = page_app(SCRIPT + CONFIG, "<html>proxy error</html>", "text/html")

    with pytest.raises(ReferenceCheckError, match=r"http://testserver/openapi.json: not JSON"):
        check_api_reference("http://testserver", fetch_with(TestClient(fixture)))


@pytest.mark.parametrize(
    ("html", "spec_body", "message"),
    [
        ("<html></html>", "{}", "names no resources"),
        (
            SCRIPT + '<script id="api-reference" data-configuration="not-json"></script>',
            "{}",
            "data-configuration is not JSON",
        ),
        (
            SCRIPT + '<script id="api-reference" data-configuration="{}"></script>',
            "{}",
            "names no spec url",
        ),
        (SCRIPT + CONFIG, "[]", "the spec is not a JSON object"),
        (SCRIPT + CONFIG, "{broken", "body is not JSON"),
    ],
    ids=["no-resources", "bad-configuration", "no-spec-url", "spec-not-object", "spec-not-json"],
)
def test_a_malformed_page_or_spec_fails(html, spec_body, message):
    fixture = page_app(html, spec_body)

    with pytest.raises(ReferenceCheckError, match=message):
        check_api_reference("http://testserver", fetch_with(TestClient(fixture)))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"health_answer": {"status": "down"}}, "not this API's answer"),
        ({"capabilities_answer": {"methods": "auto"}}, "not this API's answer"),
        ({"servers": [{}]}, "a servers entry has no url"),
    ],
    ids=["health-shape", "capabilities-shape", "server-without-url"],
)
def test_a_bad_answer_or_server_entry_fails(kwargs, message):
    with pytest.raises(ReferenceCheckError, match=message):
        check_api_reference("http://testserver", fetch_with(TestClient(reference_app(**kwargs))))


def test_the_command_line_passes_against_the_live_app(live_server):  # noqa: F811
    assert main([live_server]) == 0


def test_the_command_line_fails_when_the_reference_is_absent(live_server):  # noqa: F811
    assert main([live_server + "/absent"]) == 1


def test_the_command_line_fails_when_nothing_listens():
    assert main(["http://127.0.0.1:1"]) == 1


def test_the_command_line_needs_one_url():
    assert main([]) == 2
    assert main(["ftp://example.com"]) == 2
