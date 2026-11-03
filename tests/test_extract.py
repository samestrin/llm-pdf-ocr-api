import json
from pathlib import Path

import jsonschema
import pymupdf
import pytest

from llm_pdf_ocr import extract
from llm_pdf_ocr.extract import (
    Document,
    ErrorCode,
    Method,
    extract_pdf,
    render_json,
    render_markdown,
)
from llm_pdf_ocr.ocr import OcrError, OcrResult, TesseractNotFoundError
from llm_pdf_ocr.pdf import InvalidPdfError

FIXTURES = Path(__file__).parent / "fixtures"
MIXED = (FIXTURES / "mixed.pdf").read_bytes()
BORN_DIGITAL = (FIXTURES / "born_digital.pdf").read_bytes()

# Written by hand, not generated from the models, so a model change that breaks the contract fails.
JSON_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["page_count", "failed_pages", "pages"],
    "properties": {
        "page_count": {"type": "integer", "minimum": 0},
        "failed_pages": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["page", "status", "method", "model", "text", "confidence", "error"],
                "properties": {
                    "page": {"type": "integer", "minimum": 1},
                    "status": {"enum": ["ok", "failed"]},
                    "method": {"enum": ["text_layer", "tesseract", "vision", None]},
                    "model": {"type": ["string", "null"]},
                    "text": {"type": "string"},
                    "confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
                    "error": {
                        "oneOf": [
                            {"type": "null"},
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["code", "message"],
                                "properties": {
                                    "code": {
                                        "enum": [
                                            "text_layer_failed",
                                            "ocr_failed",
                                            "tesseract_not_found",
                                            "no_text_layer",
                                            "vision_failed",
                                        ]
                                    },
                                    "message": {"type": "string", "minLength": 1},
                                },
                            },
                        ]
                    },
                },
                "allOf": [
                    {
                        "if": {"properties": {"status": {"const": "failed"}}},
                        "then": {
                            "properties": {
                                "error": {"type": "object"},
                                "method": {"type": ["string", "null"]},
                            }
                        },
                        "else": {
                            "properties": {"error": {"type": "null"}, "method": {"type": "string"}}
                        },
                    }
                ],
            },
        },
    },
}


def fake_ocr(monkeypatch, text="ocr text", confidence=0.9, fail_on=(), exc=None):
    def run(page, language="eng"):
        if page.number + 1 in fail_on:
            raise exc or OcrError("tesseract blew up")
        return OcrResult(number=page.number + 1, text=text, confidence=confidence)

    monkeypatch.setattr(extract, "ocr_page", run)


def methods(doc: Document) -> list[str | None]:
    return [p.method for p in doc.pages]


# Pipeline


def test_born_digital_pages_use_the_text_layer(monkeypatch):
    fake_ocr(monkeypatch, fail_on=range(1, 100))  # OCR must never be called

    doc = extract_pdf(BORN_DIGITAL)

    assert doc.pages and all(p.method == Method.TEXT_LAYER and p.status == "ok" for p in doc.pages)
    assert all(p.text and p.confidence is None for p in doc.pages)
    assert doc.failed_pages == []


def test_mixed_pdf_reports_the_method_per_page(monkeypatch):
    fake_ocr(monkeypatch)

    doc = extract_pdf(MIXED)

    assert doc.page_count == len(doc.pages) >= 2
    assert Method.TEXT_LAYER in methods(doc) and Method.TESSERACT in methods(doc)
    ocr_page_ = next(p for p in doc.pages if p.method == Method.TESSERACT)
    assert ocr_page_.text == "ocr text" and ocr_page_.confidence == 0.9


def test_forced_ocr_failure_shows_as_failed_not_empty(monkeypatch):
    fake_ocr(monkeypatch, fail_on=range(1, 100))

    doc = extract_pdf(MIXED)

    failed = [p for p in doc.pages if p.status == "failed"]
    assert failed
    for page in failed:
        assert page.method == Method.TESSERACT
        assert page.text == "" and page.confidence is None
        assert page.error.code == ErrorCode.OCR_FAILED
        assert page.error.message == "tesseract blew up"
    assert doc.failed_pages == [p.number for p in failed]
    # the other pages are still delivered
    assert any(p.status == "ok" for p in doc.pages)


def test_missing_tesseract_has_its_own_error_code(monkeypatch):
    fake_ocr(
        monkeypatch, fail_on=range(1, 100), exc=TesseractNotFoundError("tesseract is not installed")
    )

    doc = extract_pdf(MIXED)

    assert {p.error.code for p in doc.pages if p.status == "failed"} == {
        ErrorCode.TESSERACT_NOT_FOUND
    }


def test_forced_text_layer_failure_shows_as_failed(monkeypatch):
    def boom(page):
        raise RuntimeError("secret internal detail /usr/lib/x.py")

    monkeypatch.setattr(extract, "read_page", boom)

    doc = extract_pdf(BORN_DIGITAL)

    assert all(p.status == "failed" and p.method == Method.TEXT_LAYER for p in doc.pages)
    assert all(p.error.code == ErrorCode.TEXT_LAYER_FAILED for p in doc.pages)
    # no internal detail or traceback is passed on
    assert all(
        "secret" not in p.error.message and "Traceback" not in p.error.message for p in doc.pages
    )


def test_invalid_pdf_is_a_document_level_error():
    with pytest.raises(InvalidPdfError):
        extract_pdf(b"not a pdf")


def test_zero_pages_gives_an_empty_document():
    doc = Document(pages=[])

    assert doc.page_count == 0 and doc.failed_pages == []


# JSON


def test_json_matches_the_schema_for_ok_and_failed_pages(monkeypatch):
    fake_ocr(monkeypatch, fail_on=[2])
    doc = extract_pdf(MIXED)

    payload = json.loads(render_json(doc))

    jsonschema.validate(payload, JSON_SCHEMA)
    assert any(p["status"] == "failed" for p in payload["pages"])
    assert any(p["status"] == "ok" for p in payload["pages"])


def test_json_for_a_failed_page_has_error_and_no_text(monkeypatch):
    fake_ocr(monkeypatch, fail_on=range(1, 100))

    payload = json.loads(render_json(extract_pdf(MIXED)))

    failed = [p for p in payload["pages"] if p["status"] == "failed"]
    assert failed[0]["error"] == {"code": "ocr_failed", "message": "tesseract blew up"}
    assert failed[0]["text"] == ""
    assert payload["failed_pages"] == [p["page"] for p in failed]


def test_the_schema_rejects_an_empty_looking_failure():
    bad = {
        "page_count": 1,
        "failed_pages": [1],
        "pages": [
            {
                "page": 1,
                "status": "failed",
                "method": "tesseract",
                "model": None,
                "text": "",
                "confidence": None,
                "error": None,
            }
        ],
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, JSON_SCHEMA)


# Markdown


def test_markdown_states_the_method_of_every_page(monkeypatch):
    fake_ocr(monkeypatch)
    doc = extract_pdf(MIXED)

    md = render_markdown(doc)

    for page in doc.pages:
        assert f"## Page {page.number}\n" in md
        assert f"Method: {page.method}" in md
    assert "Confidence: 0.90" in md
    assert "failed" not in md.lower()


def test_markdown_marks_a_failed_page_as_failed(monkeypatch):
    fake_ocr(monkeypatch, fail_on=range(1, 100))
    doc = extract_pdf(MIXED)
    number = doc.failed_pages[0]

    md = render_markdown(doc)

    assert f"> **Page {number} failed** (ocr_failed): tesseract blew up" in md
    assert f"Method: {Method.TESSERACT}" in md


# The page text itself reaches both outputs


def test_json_carries_the_text_of_every_ok_page(monkeypatch):
    fake_ocr(monkeypatch)
    doc = extract_pdf(MIXED)

    payload = json.loads(render_json(doc))

    assert [p["text"] for p in payload["pages"]] == [p.text for p in doc.pages]
    by_method = {p["method"]: p["text"] for p in payload["pages"]}
    assert by_method["tesseract"] == "ocr text"
    assert by_method["text_layer"].strip()


def test_markdown_carries_the_text_of_every_ok_page(monkeypatch):
    fake_ocr(monkeypatch)
    doc = extract_pdf(MIXED)

    md = render_markdown(doc)

    for page in doc.pages:
        section = md.split(f"## Page {page.number}\n")[1].split("\n## Page ")[0]
        assert page.text.strip() and page.text in section


def test_unexpected_ocr_exception_fails_only_that_page(monkeypatch):
    fake_ocr(
        monkeypatch, fail_on=[2], exc=pymupdf.FileDataError("corrupt page render /secret/path")
    )

    doc = extract_pdf(MIXED)

    page = doc.pages[1]
    assert page.status == "failed" and page.method == Method.TESSERACT
    assert page.text == "" and page.confidence is None
    assert page.error.code == ErrorCode.OCR_FAILED
    assert "secret" not in page.error.message
    assert doc.failed_pages == [2]
    assert doc.pages[0].status == "ok"
