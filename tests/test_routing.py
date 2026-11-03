"""Which method reads each page, with and without a vision backend, and which model."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_vision import MODEL, completion, server  # noqa: F401  (server is a fixture)

from llm_pdf_ocr import extract, vision
from llm_pdf_ocr.app import app, get_vision_config
from llm_pdf_ocr.config import ModelNotAllowedError, VisionConfig
from llm_pdf_ocr.extract import (
    ErrorCode,
    Method,
    MethodChoice,
    VisionNotConfiguredError,
    extract_pdf,
    render_json,
    render_markdown,
)
from llm_pdf_ocr.ocr import OcrResult
from llm_pdf_ocr.vision import VisionError, VisionResult

FIXTURES = Path(__file__).parent / "fixtures"
MIXED = (FIXTURES / "mixed.pdf").read_bytes()
TEXT, SCAN = "1", "2"  # mixed.pdf: page 1 has a usable text layer, page 2 is only an image

CONFIG = VisionConfig(
    base_url="http://vision.invalid/v1",
    default_model=MODEL,
    allowed_models=(MODEL, "vision-large"),
)
OFF = VisionConfig()


@pytest.fixture
def calls(monkeypatch):
    """Fake Tesseract and vision model; record which pages each one read."""
    seen = {"tesseract": [], "vision": []}

    def ocr(page, language="eng"):
        seen["tesseract"].append(page.number + 1)
        return OcrResult(number=page.number + 1, text="ocr text", confidence=0.9)

    def read(png, number, config, model=None):
        seen["vision"].append((number, model))
        return VisionResult(number=number, text="# vision text", model=config.resolve_model(model))

    monkeypatch.setattr(extract, "ocr_page", ocr)
    monkeypatch.setattr(vision, "read_image", read)
    return seen


# The routing table


ROUTES = [
    # config, choice,                  page, method,             status
    (OFF, MethodChoice.AUTO, TEXT, Method.TEXT_LAYER, "ok"),
    (OFF, MethodChoice.AUTO, SCAN, Method.TESSERACT, "ok"),
    (CONFIG, MethodChoice.AUTO, TEXT, Method.TEXT_LAYER, "ok"),
    (CONFIG, MethodChoice.AUTO, SCAN, Method.VISION, "ok"),
    (OFF, MethodChoice.TEXT_LAYER, TEXT, Method.TEXT_LAYER, "ok"),
    (OFF, MethodChoice.TEXT_LAYER, SCAN, Method.TEXT_LAYER, "failed"),
    (CONFIG, MethodChoice.TEXT_LAYER, SCAN, Method.TEXT_LAYER, "failed"),
    (OFF, MethodChoice.TESSERACT, TEXT, Method.TESSERACT, "ok"),
    (OFF, MethodChoice.TESSERACT, SCAN, Method.TESSERACT, "ok"),
    (CONFIG, MethodChoice.TESSERACT, TEXT, Method.TESSERACT, "ok"),
    (CONFIG, MethodChoice.TESSERACT, SCAN, Method.TESSERACT, "ok"),
    (CONFIG, MethodChoice.VISION, TEXT, Method.VISION, "ok"),
    (CONFIG, MethodChoice.VISION, SCAN, Method.VISION, "ok"),
]


@pytest.mark.parametrize(("config", "choice", "page", "method", "status"), ROUTES)
def test_routing_table(calls, config, choice, page, method, status):
    doc = extract_pdf(MIXED, pages=page, method=choice, vision=config)

    (result,) = doc.pages
    assert (result.method, result.status) == (method, status)
    assert calls["tesseract"] == ([int(page)] if method == Method.TESSERACT else [])
    assert [n for n, _ in calls["vision"]] == ([int(page)] if method == Method.VISION else [])
    assert result.model == (MODEL if method == Method.VISION else None)


def test_forced_vision_without_a_backend_is_refused(calls):
    with pytest.raises(VisionNotConfiguredError):
        extract_pdf(MIXED, method=MethodChoice.VISION, vision=OFF)

    assert calls == {"tesseract": [], "vision": []}


def test_forced_text_layer_on_a_scan_is_failed_not_empty(calls):
    (page,) = extract_pdf(MIXED, pages=SCAN, method=MethodChoice.TEXT_LAYER).pages

    assert page.text == ""
    assert page.error.code == ErrorCode.NO_TEXT_LAYER
    assert page.error.message


def test_no_config_is_the_epic_01_pipeline(calls):
    doc = extract_pdf(MIXED)

    assert Method.VISION not in {p.method for p in doc.pages}
    assert all(p.model is None for p in doc.pages)
    assert calls["vision"] == []


def test_auto_with_a_backend_sends_only_unreadable_pages_to_the_model(calls):
    off = extract_pdf(MIXED, vision=OFF)
    on = extract_pdf(MIXED, vision=CONFIG)

    for before, after in zip(off.pages, on.pages, strict=True):
        expected = Method.VISION if before.method == Method.TESSERACT else before.method
        assert after.method == expected
    assert calls["vision"] and all(m is None for _, m in calls["vision"])


# The model


def test_a_requested_model_on_the_list_is_used(calls):
    (page,) = extract_pdf(MIXED, pages=SCAN, model="vision-large", vision=CONFIG).pages

    assert page.model == "vision-large"
    assert calls["vision"] == [(2, "vision-large")]


@pytest.mark.parametrize("config", [CONFIG, OFF])
@pytest.mark.parametrize("choice", list(MethodChoice))
def test_a_model_outside_the_list_is_refused_before_any_page_is_read(calls, config, choice):
    with pytest.raises(ModelNotAllowedError):
        extract_pdf(MIXED, method=choice, model="some/other-model", vision=config)

    assert calls == {"tesseract": [], "vision": []}


def test_the_model_reported_by_the_endpoint_is_the_one_stated(monkeypatch, calls):
    def read(png, number, config, model=None):
        return VisionResult(number=number, text="t", model="vision-small-2026-01")

    monkeypatch.setattr(vision, "read_image", read)

    (page,) = extract_pdf(MIXED, pages=SCAN, vision=CONFIG).pages

    assert page.model == "vision-small-2026-01"


def test_a_vision_error_marks_only_that_page_failed(monkeypatch, calls):
    def read(png, number, config, model=None):
        if number == 2:
            raise VisionError("timeout", "the vision model did not answer within 60 seconds")
        return VisionResult(number=number, text="t", model=MODEL)

    monkeypatch.setattr(vision, "read_image", read)

    doc = extract_pdf(MIXED, method=MethodChoice.VISION, vision=CONFIG)

    assert doc.failed_pages == [2]
    page = doc.pages[1]
    assert (page.method, page.model, page.text) == (Method.VISION, MODEL, "")
    assert page.error.code == ErrorCode.VISION_FAILED
    assert page.error.message == "the vision model did not answer within 60 seconds"
    assert all(p.status == "ok" for p in doc.pages if p.number != 2)


def test_a_page_that_cannot_be_rendered_fails_alone(monkeypatch, calls):
    real = vision.render_page

    def render(page):
        if page.number == 1:
            raise RuntimeError("/secret/path")
        return real(page)

    monkeypatch.setattr(vision, "render_page", render)

    doc = extract_pdf(MIXED, method=MethodChoice.VISION, vision=CONFIG)

    assert doc.failed_pages == [2]
    assert "secret" not in doc.pages[1].error.message


# Output states the model


def test_json_and_markdown_state_the_model(calls):
    doc = extract_pdf(MIXED, pages="1-2", vision=CONFIG)

    pages = json.loads(render_json(doc))["pages"]
    assert [(p["method"], p["model"]) for p in pages] == [
        ("text_layer", None),
        ("vision", MODEL),
    ]
    md = render_markdown(doc)
    assert "Method: text_layer\n" in md
    assert f"Method: vision · Model: {MODEL}\n" in md


# POST /v1/extract


client = TestClient(app)


@pytest.fixture
def backend(server):  # noqa: F811
    app.dependency_overrides[get_vision_config] = lambda: server.config()
    yield server
    app.dependency_overrides.clear()


@pytest.fixture
def no_backend(calls):
    app.dependency_overrides[get_vision_config] = lambda: OFF
    yield calls
    app.dependency_overrides.clear()


def post(route="/v1/extract", **form):
    files = {"file": ("doc.pdf", MIXED, "application/pdf")}
    return client.post(route, files=files, data={"format": "json", **form})


def test_api_routes_scanned_pages_to_the_configured_model(backend):
    backend.reply = lambda body: (200, completion("# From the model", model=body["model"]))

    payload = post(pages="1-2").json()

    assert [(p["method"], p["model"]) for p in payload["pages"]] == [
        ("text_layer", None),
        ("vision", MODEL),
    ]
    assert payload["pages"][1]["text"] == "# From the model"
    assert len(backend.requests) == 1


def test_api_caller_may_force_a_method_and_pick_a_model(backend):
    backend.reply = lambda body: (200, completion("t", model=body["model"]))

    pages = post(method="vision", model="vision-large").json()["pages"]

    assert {(p["method"], p["model"]) for p in pages} == {("vision", "vision-large")}
    assert {r[2]["model"] for r in backend.requests} == {"vision-large"}


@pytest.mark.parametrize("method", ["auto", "vision", "tesseract", "text_layer"])
def test_api_refuses_a_model_outside_the_list(backend, method):
    response = post(method=method, model="some/other-model")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_allowed"
    assert backend.requests == []


def test_api_refuses_any_model_when_no_backend_is_configured(no_backend):
    response = post(model="vision-small")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_allowed"


def test_api_refuses_forced_vision_without_a_backend(no_backend):
    response = post(method="vision")

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "vision_not_configured",
            "message": "the vision backend is not configured",
        }
    }


def test_api_rejects_an_unknown_method():
    assert post(method="trocr").status_code == 422


def test_api_empty_model_field_means_the_default(backend):
    pages = post(pages="2", model="").json()["pages"]

    assert pages[0]["model"] == MODEL


def test_deprecated_ocr_uses_the_configured_model_too(backend):
    backend.reply = lambda body: (200, completion("# From the model"))

    payload = client.post("/ocr", files={"file": ("doc.pdf", MIXED, "application/pdf")}).json()

    assert "# From the model" in payload["text"]
    assert backend.requests


def test_the_vision_config_is_read_from_the_environment(monkeypatch):
    get_vision_config.cache_clear()
    monkeypatch.setenv("OCR_LLM_BASE_URL", "http://vision.invalid/v1")
    monkeypatch.setenv("OCR_LLM_DEFAULT_MODEL", "m")
    try:
        assert get_vision_config().default_model == "m"
    finally:
        get_vision_config.cache_clear()
