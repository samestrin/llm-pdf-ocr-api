"""Limits from the environment: upload size, pages per request, and the per-page timeout."""

import asyncio
import subprocess
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from bodies import without_request_id
from fastapi.testclient import TestClient

from llm_pdf_ocr import extract, ocr
from llm_pdf_ocr.app import app
from llm_pdf_ocr.config import ConfigError, Limits, load_limits
from llm_pdf_ocr.ocr import OcrResult

FIXTURES = Path(__file__).parent / "fixtures"
MIXED = (FIXTURES / "mixed.pdf").read_bytes()
SCANNED = (FIXTURES / "scanned.pdf").read_bytes()
MiB = 1024 * 1024

client = TestClient(app)


@pytest.fixture(autouse=True)
def restore_limits():
    saved = app.state.limits
    yield
    app.state.limits = saved


def set_limits(**changes) -> Limits:
    app.state.limits = replace(app.state.limits, **changes)
    return app.state.limits


def fake_ocr(monkeypatch):
    def run(page, language="eng", timeout=None):
        return OcrResult(number=page.number + 1, text="ocr text", confidence=0.9)

    monkeypatch.setattr(extract, "ocr_page", run)


def post(route, data, **form):
    return client.post(route, files={"file": ("doc.pdf", data, "application/pdf")}, data=form)


def long_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        doc.new_page().insert_text((72, 72), f"Text layer of page {n}, long enough to be usable.")
    data = doc.tobytes()
    doc.close()
    return data


# Settings


def test_defaults_when_unset():
    limits = load_limits({})
    assert limits.max_file_bytes == 50 * MiB
    assert limits.max_pages == 500
    assert limits.page_timeout == 120.0


def test_every_limit_is_read_from_the_environment():
    limits = load_limits(
        {"OCR_MAX_FILE_MB": "2.5", "OCR_MAX_PAGES": "40", "OCR_PAGE_TIMEOUT_SECONDS": "7.5"}
    )
    assert limits == Limits(max_file_bytes=int(2.5 * MiB), max_pages=40, page_timeout=7.5)


@pytest.mark.parametrize(
    "name,value",
    [
        ("OCR_MAX_FILE_MB", "big"),
        ("OCR_MAX_FILE_MB", "0"),
        ("OCR_MAX_PAGES", "1.5"),
        ("OCR_MAX_PAGES", "-3"),
        ("OCR_PAGE_TIMEOUT_SECONDS", "0"),
    ],
)
def test_bad_limits_raise(name, value):
    with pytest.raises(ConfigError, match=name):
        load_limits({name: value})


def test_the_app_reads_its_limits_from_the_environment():
    assert app.state.limits == load_limits()


# File size


@pytest.mark.parametrize("route", ["/v1/extract", "/ocr"])
def test_an_upload_at_the_limit_passes(monkeypatch, route):
    fake_ocr(monkeypatch)
    set_limits(max_file_bytes=len(MIXED))

    assert post(route, MIXED).status_code == 200


@pytest.mark.parametrize("route", ["/v1/extract", "/ocr"])
def test_an_upload_one_byte_over_the_limit_gets_413(monkeypatch, route):
    set_limits(max_file_bytes=len(MIXED) - 1)

    response = post(route, MIXED)

    assert response.status_code == 413
    assert without_request_id(response) == {
        "error": {
            "code": "file_too_large",
            "message": f"the file is larger than the limit of {len(MIXED) - 1} bytes",
        }
    }


@pytest.mark.parametrize("route", ["/v1/extract", "/ocr"])
def test_an_oversized_upload_gets_413(monkeypatch, route):
    set_limits(max_file_bytes=MiB)

    response = post(route, b"%PDF-1.7\n" + b"x" * (3 * MiB))

    assert response.status_code == 413
    assert without_request_id(response) == {
        "error": {
            "code": "file_too_large",
            "message": f"the file is larger than the limit of {MiB} bytes",
        }
    }


def call_asgi(headers, chunks):
    """Send a streamed request straight to the app; return (status, body, chunks read)."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/extract",
        "raw_path": b"/v1/extract",
        "query_string": b"",
        "root_path": "",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    read = 0
    sent = []

    async def receive():
        nonlocal read
        chunk = next(chunks, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        read += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, read


def multipart_stream(boundary: str, chunk: int):
    yield (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="doc.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n%PDF-1.7\n"
    ).encode()
    while True:  # never ends: only the cap can stop it
        yield b"x" * chunk


def test_a_streamed_upload_is_cut_off_at_the_cap(monkeypatch):
    limits = set_limits(max_file_bytes=MiB)
    chunk = 64 * 1024
    headers = [("content-type", "multipart/form-data; boundary=b0und")]

    status, body, read = call_asgi(headers, multipart_stream("b0und", chunk))

    assert status == 413
    assert b'"file_too_large"' in body
    # Reading stopped just past the cap (plus room for the form fields), not at the end.
    assert read * chunk <= limits.max_file_bytes + 2 * 64 * 1024 + chunk


def test_a_declared_length_over_the_cap_is_refused_before_reading(monkeypatch):
    set_limits(max_file_bytes=MiB)
    headers = [
        ("content-type", "multipart/form-data; boundary=b0und"),
        ("content-length", str(10 * MiB)),
    ]

    status, body, read = call_asgi(headers, multipart_stream("b0und", 64 * 1024))

    assert status == 413
    assert b'"file_too_large"' in body
    assert read == 0


def test_other_routes_are_unaffected_by_the_cap(monkeypatch):
    set_limits(max_file_bytes=1)

    assert client.get("/health").status_code == 200


# Pages


def test_a_pdf_over_the_page_limit_gets_413(monkeypatch):
    set_limits(max_pages=5)

    response = post("/v1/extract", long_pdf(30))

    assert response.status_code == 413
    assert without_request_id(response) == {
        "error": {
            "code": "too_many_pages",
            "message": "30 pages selected; the limit is 5. Ask for fewer with `pages`",
        }
    }


def test_a_long_pdf_with_a_short_pages_range_passes(monkeypatch):
    set_limits(max_pages=5)

    response = post("/v1/extract", long_pdf(30), pages="2-4,30", format="json")

    assert response.status_code == 200
    assert [p["page"] for p in response.json()["pages"]] == [2, 3, 4, 30]


def test_a_pages_range_over_the_limit_gets_413(monkeypatch):
    set_limits(max_pages=5)

    response = post("/v1/extract", long_pdf(30), pages="1-3,10-12")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_many_pages"
    assert response.json()["error"]["message"].startswith("6 pages selected; the limit is 5")


def test_the_page_limit_applies_to_the_deprecated_route(monkeypatch):
    set_limits(max_pages=3)

    response = post("/ocr", MIXED)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_many_pages"


# Per-page timeout


@pytest.mark.parametrize("method", ["auto", "tesseract"])
def test_tesseract_gets_the_configured_timeout(monkeypatch, method):
    set_limits(page_timeout=7.5)
    seen = []

    def run(cmd, **kwargs):
        seen.append(kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(ocr.subprocess, "run", run)

    assert post("/v1/extract", MIXED, method=method).status_code == 200
    assert seen and set(seen) == {7.5}  # every OCR page got the configured timeout


def test_a_page_over_the_timeout_fails_with_a_clear_error(monkeypatch):
    set_limits(page_timeout=7.5)

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(ocr.subprocess, "run", run)

    response = post("/v1/extract", MIXED, format="json")

    assert response.status_code == 200
    payload = response.json()
    failed = [p for p in payload["pages"] if p["status"] == "failed"]
    assert failed and payload["failed_pages"] == [p["page"] for p in failed]
    assert all(
        p["error"] == {"code": "page_timeout", "message": "tesseract timed out after 7.5 seconds"}
        for p in failed
    )


def test_the_cap_holds_for_an_app_that_reads_the_body_itself():
    from types import SimpleNamespace

    from llm_pdf_ocr.limits import UploadCap

    async def reads_everything(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"read it all"})

    state = SimpleNamespace(limits=Limits(max_file_bytes=1024))
    capped = UploadCap(reads_everything)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"x" * 4096, "more_body": True}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "headers": [], "app": SimpleNamespace(state=state)}
    asyncio.run(capped(scope, receive, send))

    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413]
    assert b"read it all" not in b"".join(m.get("body", b"") for m in sent)


def test_startup_and_shutdown_pass_through_the_cap():
    with TestClient(app) as started:
        assert started.get("/health").status_code == 200
