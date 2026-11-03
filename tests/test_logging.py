"""Logs carry no page text; error bodies and responses carry a request id."""

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_pdf_ocr.app import app

FIXTURES = Path(__file__).parent / "fixtures"
MIXED = (FIXTURES / "mixed.pdf").read_bytes()

client = TestClient(app, raise_server_exceptions=False)


def post(path="/v1/extract", data=None, content=MIXED):
    return client.post(path, files={"file": ("a.pdf", content, "application/pdf")}, data=data or {})


def test_no_page_text_in_logs(caplog):
    caplog.set_level(logging.DEBUG)
    text = post(data={"format": "json"}).json()["pages"][0]["text"]
    assert text.strip()
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 12]
    assert lines
    for line in lines:
        assert line not in caplog.text
    assert "status=200" in caplog.text


def test_no_page_text_in_logs_when_a_failure_carries_it(caplog, monkeypatch):
    secret = "SECRET-PAGE-TEXT-9917"

    def boom(*args, **kwargs):
        raise RuntimeError(secret)

    import llm_pdf_ocr.app as appmod

    monkeypatch.setattr(appmod, "extract_pdf", boom)
    caplog.set_level(logging.DEBUG)
    response = post()
    assert response.status_code == 500
    assert secret not in caplog.text
    assert secret not in response.text
    assert "RuntimeError" in caplog.text


def test_every_response_has_a_request_id():
    response = client.get("/health")
    assert len(response.headers["x-request-id"]) == 32
    assert response.headers["x-request-id"] != client.get("/health").headers["x-request-id"]


def test_client_supplied_id_is_ignored():
    response = client.get("/health", headers={"X-Request-ID": "forged\nline"})
    assert response.headers["x-request-id"] != "forged\nline"


@pytest.mark.parametrize("content", [b"not a pdf"])
def test_error_body_has_the_request_id(content):
    response = post(content=content)
    assert response.status_code == 400
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_unhandled_error_is_500_with_the_request_id(monkeypatch):
    import llm_pdf_ocr.app as appmod

    def boom(*args, **kwargs):
        raise RuntimeError("x")

    monkeypatch.setattr(appmod, "extract_pdf", boom)
    response = post()
    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["request_id"] == response.headers["x-request-id"]
