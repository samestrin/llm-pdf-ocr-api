# ruff: noqa: F811  (`server` is a fixture imported from test_vision)
"""A vision model that times out, is rate limited or sends a bad reply.

The page is failed with its reason, or, only when the operator allows it, read by
Tesseract instead and marked as a fallback. Tests use the fake OpenAI-compatible server.
"""

import json
import logging

import jsonschema
import pytest
from fastapi.testclient import TestClient
from test_extract import JSON_SCHEMA
from test_routing import MIXED, SCAN
from test_vision import API_KEY, MODEL, assert_no_secrets, server  # noqa: F401  (fixture)

from llm_pdf_ocr import extract
from llm_pdf_ocr.app import app, get_vision_config
from llm_pdf_ocr.extract import (
    ErrorCode,
    Method,
    MethodChoice,
    extract_pdf,
    render_json,
    render_markdown,
)
from llm_pdf_ocr.ocr import OcrError, OcrResult


def slow(server):
    server.delay = 0.5
    return {"timeout": 0.1}


def rate_limited(server):
    server.reply = lambda body: (429, {"error": {"message": f"slow down, {API_KEY}"}})
    return {}


def bad_response(server):
    server.reply = lambda body: (200, b"<html>not json</html>")
    return {}


FAILURES = [
    pytest.param(slow, "the vision model did not answer within 0.1 seconds", id="timeout"),
    pytest.param(rate_limited, "the vision model is rate limited (HTTP 429)", id="429"),
    pytest.param(bad_response, "the vision model sent an unreadable reply", id="bad-response"),
]


timeouts: list[float | None] = []


@pytest.fixture
def tesseract(monkeypatch):
    """A fake Tesseract that records the pages it read (and, in `timeouts`, the time
    it was given for each)."""
    timeouts.clear()
    seen = []

    def ocr(page, language="eng", timeout=None):
        seen.append(page.number + 1)
        timeouts.append(timeout)
        return OcrResult(number=page.number + 1, text="ocr text", confidence=0.8)

    monkeypatch.setattr(extract, "ocr_page", ocr)
    return seen


# Fallback off (the default): the page is failed with its reason


@pytest.mark.parametrize(("failure", "reason"), FAILURES)
def test_a_failure_marks_the_page_failed_with_its_reason(server, tesseract, failure, reason):
    config = server.config(**failure(server))

    doc = extract_pdf(MIXED, pages="1-2", vision=config)

    text_page, scan = doc.pages
    assert text_page.status == "ok"
    assert doc.failed_pages == [2]
    assert (scan.method, scan.model, scan.text, scan.fallback) == (Method.VISION, MODEL, "", None)
    assert scan.error.code == ErrorCode.VISION_FAILED
    assert scan.error.message == reason
    assert tesseract == []


def test_fallback_is_off_by_default(server):
    assert server.config().fallback_to_tesseract is False


# Fallback on: Tesseract reads the page, and the page says so


@pytest.mark.parametrize(("failure", "reason"), FAILURES)
def test_with_fallback_allowed_tesseract_reads_the_page(server, tesseract, failure, reason):
    config = server.config(fallback_to_tesseract=True, **failure(server))

    doc = extract_pdf(MIXED, pages="1-2", vision=config)

    scan = doc.pages[1]
    assert doc.failed_pages == []
    assert (scan.status, scan.method, scan.model) == ("ok", Method.TESSERACT, None)
    assert (scan.text, scan.confidence, scan.error) == ("ocr text", 0.8, None)
    assert scan.fallback.method == Method.VISION
    assert scan.fallback.model == MODEL
    assert scan.fallback.error.code == ErrorCode.VISION_FAILED
    assert scan.fallback.error.message == reason
    assert tesseract == [2]


def test_fallback_applies_to_a_forced_vision_method_too(server, tesseract):
    config = server.config(fallback_to_tesseract=True, **rate_limited(server))

    doc = extract_pdf(MIXED, pages="1-2", method=MethodChoice.VISION, vision=config)

    assert [p.method for p in doc.pages] == [Method.TESSERACT, Method.TESSERACT]
    assert all(p.fallback is not None for p in doc.pages)
    assert tesseract == [1, 2]


def test_a_page_the_model_reads_has_no_fallback(server, tesseract):
    doc = extract_pdf(MIXED, pages=SCAN, vision=server.config(fallback_to_tesseract=True))

    (page,) = doc.pages
    assert (page.method, page.fallback) == (Method.VISION, None)
    assert tesseract == []


def test_a_page_that_cannot_be_rendered_falls_back_too(server, tesseract, monkeypatch):
    def render(page):
        raise RuntimeError("/secret/path")

    monkeypatch.setattr(extract.vision_model, "render_page", render)

    (page,) = extract_pdf(MIXED, pages=SCAN, vision=server.config(fallback_to_tesseract=True)).pages

    assert page.method == Method.TESSERACT
    assert page.fallback.error.message == "the page could not be rendered"
    assert server.requests == []


def test_the_fallback_gets_the_per_page_tesseract_timeout(server, tesseract):
    config = server.config(fallback_to_tesseract=True, **rate_limited(server))

    extract_pdf(MIXED, pages=SCAN, vision=config, page_timeout=7.5)

    assert tesseract == [int(SCAN)]
    assert timeouts == [7.5]


def test_when_the_fallback_fails_too_the_page_is_failed_with_both_reasons(server, monkeypatch):
    def ocr(page, language="eng", timeout=None):
        raise OcrError("tesseract could not read the page")

    monkeypatch.setattr(extract, "ocr_page", ocr)
    config = server.config(fallback_to_tesseract=True, **rate_limited(server))

    (page,) = extract_pdf(MIXED, pages=SCAN, vision=config).pages

    assert (page.status, page.method, page.text) == ("failed", Method.TESSERACT, "")
    assert page.error.code == ErrorCode.OCR_FAILED
    assert page.error.message == "tesseract could not read the page"
    assert page.fallback.error.message == "the vision model is rate limited (HTTP 429)"


# The output says so


def test_json_states_the_fallback(server, tesseract):
    config = server.config(fallback_to_tesseract=True, **rate_limited(server))

    payload = json.loads(render_json(extract_pdf(MIXED, pages="1-2", vision=config)))
    pages = payload["pages"]

    jsonschema.validate(payload, JSON_SCHEMA)

    assert pages[0]["fallback"] is None
    assert pages[1]["method"] == "tesseract"
    assert pages[1]["fallback"] == {
        "method": "vision",
        "model": MODEL,
        "error": {
            "code": "vision_failed",
            "message": "the vision model is rate limited (HTTP 429)",
        },
    }


def test_markdown_states_the_fallback(server, tesseract):
    config = server.config(fallback_to_tesseract=True, **rate_limited(server))

    md = render_markdown(extract_pdf(MIXED, pages=SCAN, vision=config))

    assert md == (
        "## Page 2\n\n"
        "Method: tesseract · Confidence: 0.80\n\n"
        f"> Read with Tesseract because vision ({MODEL}) failed (vision_failed): "
        "the vision model is rate limited (HTTP 429)\n\n"
        "ocr text\n"
    )


# POST /v1/extract

client = TestClient(app)


def post(**form):
    files = {"file": ("doc.pdf", MIXED, "application/pdf")}
    return client.post("/v1/extract", files=files, data={"format": "json", **form})


@pytest.mark.parametrize(("failure", "reason"), FAILURES)
@pytest.mark.parametrize("fallback", [False, True])
def test_api_failures_never_leak_the_key_or_url(
    server, tesseract, caplog, failure, reason, fallback
):
    config = server.config(fallback_to_tesseract=fallback, **failure(server))
    app.dependency_overrides[get_vision_config] = lambda: config

    with caplog.at_level(logging.DEBUG):
        json_reply = post(pages="1-2")
        md_reply = post(pages="1-2", format="markdown")

    assert json_reply.status_code == md_reply.status_code == 200
    page = json_reply.json()["pages"][1]
    if fallback:
        assert (page["status"], page["method"]) == ("ok", "tesseract")
        assert page["fallback"]["error"]["message"] == reason
    else:
        assert (page["status"], page["method"]) == ("failed", "vision")
        assert page["error"]["message"] == reason
    assert reason in md_reply.text
    for text in (json_reply.text, md_reply.text, caplog.text):
        assert_no_secrets(text, server)
