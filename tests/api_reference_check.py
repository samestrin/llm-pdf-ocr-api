"""The API reference's "try it" must reach the API, from the page's own origin.

Scalar builds each request URL from the spec's `servers`, resolved against the
page's origin; with no `servers` entry it uses the origin's root. A reference
moved behind a path prefix without a `servers` entry therefore points "try it"
at the wrong place, silently. This check walks the page the way the browser
does -- `/docs`, every resource it names, the spec at the configured URL, then
one GET path per declared server (the root when none) -- and fails on the first
mismatch.

Standard library only, so the same file runs two ways:

* pytest imports `check_api_reference` (see `test_api_reference_check.py`);
* CI's `try-it` job runs it against the compose stack with the runner's system
  python: `python3 tests/api_reference_check.py http://localhost:5000`.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from html import unescape
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

# The GET paths "try it" is proven against, and what each answer must carry to
# count as an answer from this API (not a proxy's error page).
GET_PATHS = ("/v1/capabilities", "/health")


class Response(NamedTuple):
    status: int
    content_type: str
    body: str


Fetch = Callable[[str], Response]


class ReferenceCheckError(AssertionError):
    """The reference would not load, or "try it" would not reach the API."""


def _origin(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def _answers_from_the_api(path: str, answer: object) -> bool:
    if path == "/health":
        return isinstance(answer, dict) and answer.get("status") == "ok"
    return isinstance(answer, dict) and isinstance(answer.get("methods"), list)


def check_api_reference(base_url: str, fetch: Fetch) -> list[str]:
    """Check the reference served at `base_url` + `/docs`; return every URL fetched.

    `fetch(url)` answers with `Response(status, content_type, body)`. Raises
    `ReferenceCheckError` on the first step that would break the page or "try it".
    """
    checked: list[str] = []

    def get(url: str) -> Response:
        response = fetch(url)
        checked.append(url)
        if response.status != 200:
            raise ReferenceCheckError(f"{url}: HTTP {response.status}, wanted 200")
        return response

    def get_json(url: str) -> object:
        response = get(url)
        if not response.content_type.startswith("application/json"):
            raise ReferenceCheckError(f"{url}: not JSON (Content-Type {response.content_type!r})")
        try:
            return json.loads(response.body)
        except ValueError as error:
            raise ReferenceCheckError(f"{url}: body is not JSON: {error}") from error

    page_url = base_url.rstrip("/") + "/docs"
    origin = _origin(page_url)
    page = get(page_url)
    if not page.content_type.startswith("text/html"):
        raise ReferenceCheckError(f"{page_url}: not HTML (Content-Type {page.content_type!r})")

    # Every resource the page names loads from this origin, and loads at all.
    assets = re.findall(r'(?:src|href)="([^"]+)"', page.body)
    if not assets:
        raise ReferenceCheckError(f"{page_url}: names no resources; is this the reference page?")
    for asset in assets:
        url = urljoin(page_url, asset)
        if _origin(url) != origin:
            raise ReferenceCheckError(f"{page_url}: resource {asset!r} leaves the origin ({url})")
        get(url)

    # The spec, where the page's configuration says it is.
    attributes = re.findall(r'data-configuration="([^"]*)"', page.body)
    if len(attributes) != 1:
        raise ReferenceCheckError(
            f"{page_url}: expected one data-configuration attribute, found {len(attributes)}"
        )
    try:
        configuration = json.loads(unescape(attributes[0]))
    except ValueError as error:
        raise ReferenceCheckError(f"{page_url}: data-configuration is not JSON: {error}") from error
    if configuration.get("proxyUrl"):
        raise ReferenceCheckError(
            f"{page_url}: proxyUrl {configuration['proxyUrl']!r} would send requests "
            "to another origin"
        )
    if not configuration.get("url"):
        raise ReferenceCheckError(f"{page_url}: data-configuration names no spec url")
    spec_url = urljoin(page_url, configuration["url"])
    if _origin(spec_url) != origin:
        raise ReferenceCheckError(
            f"{page_url}: spec url {configuration['url']!r} leaves the origin ({spec_url})"
        )
    spec = get_json(spec_url)
    if not isinstance(spec, dict):
        raise ReferenceCheckError(f"{spec_url}: the spec is not a JSON object")

    # "try it": each server the spec declares, resolved against the page's
    # origin as Scalar does; no servers means the root.
    for server in spec.get("servers") or [{"url": "/"}]:
        url = server.get("url") if isinstance(server, dict) else None
        if not url:
            raise ReferenceCheckError(f"{spec_url}: a servers entry has no url: {server!r}")
        server_url = urljoin(origin + "/", url)
        for path in GET_PATHS:
            request_url = server_url.rstrip("/") + path
            answer = get_json(request_url)
            if not _answers_from_the_api(path, answer):
                raise ReferenceCheckError(
                    f"{request_url}: JSON is not this API's answer to {path}: {str(answer)[:100]}"
                )
    return checked


def _fetch_over_http(url: str) -> Response:
    from urllib.error import HTTPError
    from urllib.request import urlopen

    try:
        with urlopen(url, timeout=10) as response:
            return Response(
                response.status,
                response.headers.get("Content-Type", ""),
                response.read().decode("utf-8", "replace"),
            )
    except HTTPError as error:
        return Response(
            error.code,
            error.headers.get("Content-Type", ""),
            error.read().decode("utf-8", "replace"),
        )
    except OSError as error:
        raise ReferenceCheckError(f"{url}: {error}") from error


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not args[0].startswith(("http://", "https://")):
        print(
            "usage: python3 tests/api_reference_check.py http://localhost:5000",
            file=sys.stderr,
        )
        return 2
    try:
        checked = check_api_reference(args[0], _fetch_over_http)
    except ReferenceCheckError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    print(f"OK: the reference and its API answers checked over {len(checked)} URLs:")
    for url in checked:
        print(f"  {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
