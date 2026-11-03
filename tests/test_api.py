import shutil
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

from llm_pdf_ocr import extract
from llm_pdf_ocr.app import app
from llm_pdf_ocr.ocr import OcrError, OcrResult

FIXTURES = Path(__file__).parent / "fixtures"
BORN_DIGITAL = (FIXTURES / "born_digital.pdf").read_bytes()
SCANNED = (FIXTURES / "scanned.pdf").read_bytes()
MIXED = (FIXTURES / "mixed.pdf").read_bytes()

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is not installed"
)

client = TestClient(app)


def fake_ocr(monkeypatch, text="ocr text", fail_on=()):
    def run(page, language="eng"):
        if page.number + 1 in fail_on:
            raise OcrError("tesseract blew up")
        return OcrResult(number=page.number + 1, text=text, confidence=0.9)

    monkeypatch.setattr(extract, "ocr_page", run)


def post(route, data=MIXED, **form):
    return client.post(route, files={"file": ("doc.pdf", data, "application/pdf")}, data=form)


def encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    doc.close()
    return data


# POST /v1/extract


def test_extract_defaults_to_markdown(monkeypatch):
    fake_ocr(monkeypatch)

    response = post("/v1/extract")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    md = response.text
    assert "## Page 1\n" in md and "Method: text_layer" in md
    assert "Method: tesseract" in md and "ocr text" in md


def test_extract_json_names_the_method_of_every_page(monkeypatch):
    fake_ocr(monkeypatch)

    response = post("/v1/extract", format="json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()
    assert payload["page_count"] == len(payload["pages"]) == 4
    assert payload["failed_pages"] == []
    assert all(p["status"] == "ok" and p["text"] for p in payload["pages"])
    assert {p["method"] for p in payload["pages"]} == {"text_layer", "tesseract"}


def test_extract_born_digital_needs_no_ocr(monkeypatch):
    fake_ocr(monkeypatch, fail_on=range(1, 100))  # OCR must never be called

    payload = post("/v1/extract", data=BORN_DIGITAL, format="json").json()

    assert payload["failed_pages"] == []
    assert all(p["method"] == "text_layer" and p["text"] for p in payload["pages"])


@needs_tesseract
def test_extract_scanned_pdf_with_real_tesseract():
    payload = post("/v1/extract", data=SCANNED, format="json").json()

    assert payload["failed_pages"] == []
    assert all(p["method"] == "tesseract" for p in payload["pages"])
    assert "Scanned page text" in payload["pages"][0]["text"]


def test_extract_reports_a_failed_page_as_failed(monkeypatch):
    fake_ocr(monkeypatch, fail_on=[2])

    response = post("/v1/extract", format="json")

    assert response.status_code == 200  # the other pages are still delivered
    payload = response.json()
    assert payload["failed_pages"] == [2]
    page = payload["pages"][1]
    assert page["status"] == "failed" and page["text"] == ""
    assert page["error"] == {"code": "ocr_failed", "message": "tesseract blew up"}


def test_extract_markdown_marks_a_failed_page(monkeypatch):
    fake_ocr(monkeypatch, fail_on=[2])

    md = post("/v1/extract").text

    assert "> **Page 2 failed** (ocr_failed): tesseract blew up" in md


@pytest.mark.parametrize(
    ("pages", "expected"),
    [("2", [2]), ("1,3", [1, 3]), ("2-4", [2, 3, 4]), (" 3-4, 1 ", [1, 3, 4]), ("1-2,2", [1, 2])],
)
def test_extract_page_range(monkeypatch, pages, expected):
    fake_ocr(monkeypatch)

    payload = post("/v1/extract", format="json", pages=pages).json()

    assert [p["page"] for p in payload["pages"]] == expected
    assert payload["page_count"] == len(expected)


def test_extract_page_range_in_markdown_keeps_page_numbers(monkeypatch):
    fake_ocr(monkeypatch)

    md = post("/v1/extract", pages="3").text

    assert md.startswith("## Page 3\n") and "## Page 1" not in md


def test_extract_page_range_reads_only_those_pages(monkeypatch):
    fake_ocr(monkeypatch, fail_on=[2])  # page 2 would fail if it were read

    payload = post("/v1/extract", format="json", pages="1,3").json()

    assert payload["failed_pages"] == []


@pytest.mark.parametrize("pages", ["abc", "0", "3-1", "1-", "-2", "1,,2", "1.5", ","])
def test_extract_rejects_a_malformed_page_range(pages):
    response = post("/v1/extract", pages=pages)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_page_range"


def test_extract_rejects_pages_beyond_the_pdf():
    response = post("/v1/extract", pages="2-9")

    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "page_out_of_range", "message": "page 5 is not in a 4-page PDF"}
    }


def test_extract_rejects_an_unknown_format():
    response = post("/v1/extract", format="html")

    assert response.status_code == 422


def test_extract_needs_a_file():
    assert client.post("/v1/extract", data={"format": "json"}).status_code == 422


# Bad PDFs, on both routes


@pytest.mark.parametrize("route", ["/v1/extract", "/ocr"])
@pytest.mark.parametrize("data", [b"not a pdf", b"", BORN_DIGITAL[: len(BORN_DIGITAL) // 3]])
def test_a_bad_pdf_is_rejected_cleanly(route, data):
    response = post(route, data=data)

    assert response.status_code == 400
    assert response.json() == {"error": {"code": "invalid_pdf", "message": "not a readable PDF"}}
    assert "Traceback" not in response.text


@pytest.mark.parametrize("route", ["/v1/extract", "/ocr"])
def test_an_encrypted_pdf_has_its_own_error(route):
    response = post(route, data=encrypted_pdf())

    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "encrypted_pdf", "message": "the PDF is encrypted"}
    }


# POST /ocr (deprecated alias for 0.0.1)


def test_ocr_returns_text(monkeypatch):
    fake_ocr(monkeypatch)

    response = post("/ocr")

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"text", "failed_pages"}
    assert "ocr text" in payload["text"]
    assert payload["failed_pages"] == []


def test_ocr_text_joins_pages_in_order(monkeypatch):
    fake_ocr(monkeypatch)
    pages = post("/v1/extract", format="json").json()["pages"]

    text = post("/ocr").json()["text"]

    assert text == "\n\n".join(p["text"] for p in pages)


def test_ocr_says_which_pages_failed(monkeypatch):
    fake_ocr(monkeypatch, fail_on=[2])

    payload = post("/ocr").json()

    assert payload["failed_pages"] == [2]
    assert "ocr text" in payload["text"]  # page 4 is still there


def test_ocr_ignores_the_old_model_fields(monkeypatch):
    fake_ocr(monkeypatch)

    response = post(
        "/ocr", model="microsoft/trocr-base-printed", threshold_value="150", min_area="50"
    )

    assert response.status_code == 200
    assert "ocr text" in response.json()["text"]


def test_ocr_is_marked_deprecated(monkeypatch):
    fake_ocr(monkeypatch)

    assert post("/ocr").headers["deprecation"] == "true"
    assert client.get("/openapi.json").json()["paths"]["/ocr"]["post"]["deprecated"] is True


# Docs


def test_openapi_docs_are_served():
    assert client.get("/docs").status_code == 200
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/v1/extract", "/ocr", "/health"} <= set(paths)
