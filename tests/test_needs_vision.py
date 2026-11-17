"""Per-page "needs vision", and `POST /v1/jobs/{id}/pages/{n}/vision`.

A page with no usable text layer that Tesseract failed on, or read with a confidence under
`OCR_LOW_CONFIDENCE`, is marked `needs_vision` with the reason. Its job keeps the PDF until
it expires, so the page can be read again with a vision model using the visitor's own key or
demo code, and the result for that page is replaced. Tests use the bundled scanned fixture
(two scanned pages, neither with a usable text layer) and a fake OpenAI-compatible provider.
"""

import base64
import json
import shutil
import struct
import threading
import time
from pathlib import Path

import jsonschema
import pymupdf
import pytest
from fastapi.testclient import TestClient
from test_extract import JSON_SCHEMA
from test_openapi_responses import check, resolve, schema
from test_presets import FAKE_KEY, fake_provider  # noqa: F401  (fixture)
from test_vision import MODEL, completion

from llm_pdf_ocr import extract, vision
from llm_pdf_ocr.app import app
from llm_pdf_ocr.config import DEFAULT_LOW_CONFIDENCE, ConfigError, load_limits
from llm_pdf_ocr.extract import (
    ErrorCode,
    Method,
    MethodChoice,
    extract_pdf,
    render_json,
    render_markdown,
)
from llm_pdf_ocr.jobs import JobStatus, JobStore
from llm_pdf_ocr.ocr import OcrError, OcrResult

SCANNED = (Path(__file__).parent / "fixtures" / "scanned.pdf").read_bytes()
SCAN, READ_WELL = 1, 2  # with `scores` below: Tesseract scores page 1 low and page 2 well
KEY = {"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY}
WAIT = 5

client = TestClient(app)

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is not installed"
)


def tesseract_scores(monkeypatch, confidence: float | dict[int, float] | None) -> None:
    """A fake Tesseract that reads every page with `confidence` (or, from a dict, each page
    with its own), or fails when it is None."""

    def ocr(page, language="eng", timeout=None):
        score = confidence
        if isinstance(confidence, dict):
            score = confidence[page.number + 1]
        if score is None:
            raise OcrError("tesseract failed")
        return OcrResult(number=page.number + 1, text="faint words", confidence=score)

    monkeypatch.setattr(extract, "ocr_page", ocr)


# --- marking a page -------------------------------------------------------------------


def test_the_low_confidence_setting_defaults_to_0_6():
    assert DEFAULT_LOW_CONFIDENCE == 0.6
    assert load_limits({}).low_confidence == 0.6
    assert load_limits({"OCR_LOW_CONFIDENCE": "0.8"}).low_confidence == 0.8


@pytest.mark.parametrize("value", ["0", "-0.1", "1.5", "high"])
def test_a_low_confidence_outside_0_to_1_is_a_config_error(value):
    with pytest.raises(ConfigError, match="OCR_LOW_CONFIDENCE"):
        load_limits({"OCR_LOW_CONFIDENCE": value})


def test_a_scanned_page_tesseract_scored_low_is_marked_with_the_reason(monkeypatch):
    tesseract_scores(monkeypatch, {SCAN: 0.42, READ_WELL: 0.9})

    doc = extract_pdf(SCANNED)

    scan, text = doc.pages
    assert (scan.status, scan.method, scan.text) == ("ok", Method.TESSERACT, "faint words")
    assert scan.needs_vision is True
    assert scan.needs_vision_reason == (
        "the page has no usable text layer and Tesseract's confidence was 0.42, under 0.6"
    )
    assert (text.method, text.needs_vision, text.needs_vision_reason) == (
        Method.TESSERACT,
        False,
        None,
    )
    assert doc.needs_vision


def test_a_scanned_page_tesseract_failed_on_is_marked_with_the_reason(monkeypatch):
    tesseract_scores(monkeypatch, None)

    scan = extract_pdf(SCANNED, pages="1").pages[0]

    assert (scan.status, scan.error.code) == ("failed", ErrorCode.OCR_FAILED)
    assert scan.needs_vision is True
    assert scan.needs_vision_reason == (
        "the page has no usable text layer and Tesseract could not read it"
    )


def test_a_page_tesseract_read_well_is_not_marked(monkeypatch):
    tesseract_scores(monkeypatch, 0.9)

    doc = extract_pdf(SCANNED)

    assert [p.needs_vision for p in doc.pages] == [False, False]
    assert not doc.needs_vision


def test_the_threshold_is_the_one_given(monkeypatch):
    tesseract_scores(monkeypatch, 0.9)

    assert extract_pdf(SCANNED, pages="1", low_confidence=0.95).pages[0].needs_vision
    assert not extract_pdf(SCANNED, pages="1", low_confidence=0.9).pages[0].needs_vision


def test_a_page_read_by_forced_tesseract_or_text_layer_is_not_marked(monkeypatch):
    """Only `auto` routes by the text layer; a forced method is what the caller chose."""
    tesseract_scores(monkeypatch, 0.1)

    forced = [
        extract_pdf(SCANNED, pages="1", method=m).pages[0]
        for m in (MethodChoice.TESSERACT, MethodChoice.TEXT_LAYER)
    ]

    assert [p.needs_vision for p in forced] == [False, False]


@needs_tesseract
def test_the_real_scanned_fixture_is_marked_under_a_strict_threshold():
    scan = extract_pdf(SCANNED, pages="1", low_confidence=1.0).pages[0]

    assert scan.method == Method.TESSERACT
    assert scan.needs_vision is True


def test_the_mark_is_in_the_json_and_the_markdown(monkeypatch):
    tesseract_scores(monkeypatch, {SCAN: 0.42, READ_WELL: 0.9})
    doc = extract_pdf(SCANNED)

    payload = json.loads(render_json(doc))
    jsonschema.validate(payload, JSON_SCHEMA)
    assert payload["pages"][0]["needs_vision"] is True
    assert payload["pages"][0]["needs_vision_reason"].startswith("the page has no usable")
    assert payload["pages"][1]["needs_vision"] is False
    markdown = render_markdown(doc)
    assert "> Needs a vision model: the page has no usable text layer" in markdown
    assert markdown.count("Needs a vision model") == 1


# --- the job keeps the PDF only while a page needs vision -----------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    saved = app.state.jobs
    app.state.jobs = JobStore(max_jobs=3, ttl=60, clock=clock)
    yield app.state.jobs
    app.state.jobs.shutdown()
    app.state.jobs = saved


def job_for(store: JobStore, data: bytes = SCANNED, **form) -> str:
    response = client.post(
        "/v1/jobs", files={"file": ("scan.pdf", data, "application/pdf")}, data=form
    )
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert store.get(job_id).finished.wait(WAIT)
    return job_id


def test_a_job_with_a_marked_page_keeps_its_pdf_until_it_expires(monkeypatch, store, clock):
    tesseract_scores(monkeypatch, 0.42)
    job = store.get(job_for(store, format="json"))

    assert job.status is JobStatus.DONE
    assert job.data == SCANNED

    clock.now += 60
    assert store.get(job.id) is None
    assert job.data is None


def test_a_job_with_no_marked_page_drops_its_pdf_when_it_ends(monkeypatch, store):
    tesseract_scores(monkeypatch, 0.9)
    job = store.get(job_for(store))

    assert job.status is JobStatus.DONE
    assert job.data is None


def test_a_failed_job_drops_its_pdf(store):
    job = store.submit(SCANNED, lambda data, job: 1 / 0, keep_data=lambda result: True)
    assert job.finished.wait(WAIT)

    assert (job.status, job.data) == (JobStatus.FAILED, None)


# --- POST /v1/jobs/{id}/pages/{n}/vision ----------------------------------------------


def read_with_vision(job_id: str, number: int = SCAN, headers=KEY):
    return client.post(f"/v1/jobs/{job_id}/pages/{number}/vision", headers=headers)


@pytest.fixture
def marked(monkeypatch, store):
    """A finished JSON job on the scanned fixture whose page 1 needs vision and page 2 not."""
    tesseract_scores(monkeypatch, {SCAN: 0.42, READ_WELL: 0.9})
    return job_for(store, format="json")


def png_size(png: bytes) -> tuple[int, int]:
    return struct.unpack(">II", png[16:24])


def test_the_route_reads_the_page_and_replaces_its_result(
    marked,
    store,
    fake_provider,  # noqa: F811
):
    response = read_with_vision(marked)

    assert response.status_code == 200
    assert response.json() == {
        "page": SCAN,
        "status": "ok",
        "method": "vision",
        "model": MODEL,  # as the provider names it
        "text": "# Page",
        "confidence": None,
        "error": None,
        "fallback": None,
        "needs_vision": False,
        "needs_vision_reason": None,
    }
    (_, headers, body) = fake_provider.requests[0]
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"
    url = body["messages"][0]["content"][1]["image_url"]["url"]
    png = base64.b64decode(url.removeprefix("data:image/png;base64,"))
    with pymupdf.open(stream=SCANNED, filetype="pdf") as doc:
        at_150 = doc[SCAN - 1].get_pixmap(dpi=150)
    assert vision.RENDER_DPI == 150
    assert png_size(png) == (at_150.width, at_150.height)

    job = client.get(f"/v1/jobs/{marked}").json()
    assert job["result"]["pages"][0] == response.json()
    assert job["result"]["pages"][1]["method"] == "tesseract"
    assert job["result"]["failed_pages"] == []
    assert store.get(marked).data is None  # no page needs vision now: the PDF is dropped
    assert FAKE_KEY not in str(job)


def test_a_failed_page_read_with_vision_is_ok_in_the_job_progress(
    monkeypatch,
    store,
    fake_provider,  # noqa: F811
):
    tesseract_scores(monkeypatch, {SCAN: None, READ_WELL: 0.9})
    job_id = job_for(store, format="json")
    assert client.get(f"/v1/jobs/{job_id}").json()["result"]["failed_pages"] == [SCAN]

    assert read_with_vision(job_id).status_code == 200

    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["result"]["failed_pages"] == []
    assert {"page": SCAN, "status": "ok"} in job["pages"]


def test_a_markdown_job_shows_the_page_read_with_vision(
    monkeypatch,
    store,
    fake_provider,  # noqa: F811
):
    tesseract_scores(monkeypatch, {SCAN: 0.42, READ_WELL: 0.9})
    job_id = job_for(store)

    assert "Needs a vision model" in client.get(f"/v1/jobs/{job_id}").json()["result"]
    read_with_vision(job_id)

    result = client.get(f"/v1/jobs/{job_id}").json()["result"]
    assert f"Method: vision · Model: {MODEL}" in result
    assert "Needs a vision model" not in result


def test_without_credentials_the_route_is_401_credentials_required(marked, store):
    response = read_with_vision(marked, headers={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "credentials_required"
    assert store.get(marked).data == SCANNED  # still held for a read with a key


def test_a_page_that_does_not_need_vision_is_refused(marked, fake_provider):  # noqa: F811
    response = read_with_vision(marked, number=READ_WELL)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "vision_not_needed"
    assert fake_provider.requests == []


def test_a_page_already_read_with_vision_is_refused(marked, fake_provider):  # noqa: F811
    assert read_with_vision(marked).status_code == 200

    again = read_with_vision(marked)

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "vision_not_needed"
    assert len(fake_provider.requests) == 1


def test_a_page_the_job_did_not_read_is_404(marked, fake_provider):  # noqa: F811
    response = read_with_vision(marked, number=9)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "page_not_found"


def test_after_the_job_expired_the_route_is_404(marked, clock, fake_provider):  # noqa: F811
    clock.now += 60

    response = read_with_vision(marked)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"
    assert fake_provider.requests == []


def test_an_unknown_job_is_404(store):
    response = read_with_vision("no-such-job")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"


def test_a_job_still_running_is_refused(store):
    release = threading.Event()
    job = store.submit(SCANNED, lambda data, job: release.wait(WAIT), format="json")
    try:
        response = read_with_vision(job.id)
    finally:
        release.set()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_not_done"


def test_a_second_read_of_a_page_being_read_is_refused(marked, fake_provider):  # noqa: F811
    fake_provider.delay = 0.5
    first: list = []
    reader = threading.Thread(target=lambda: first.append(read_with_vision(marked)))
    reader.start()
    deadline = time.monotonic() + WAIT
    while not fake_provider.requests and time.monotonic() < deadline:
        time.sleep(0.01)

    second = read_with_vision(marked)
    reader.join(WAIT)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "page_busy"
    assert first[0].status_code == 200
    assert len(fake_provider.requests) == 1


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (
            lambda body: (401, {"error": f"bad key {FAKE_KEY}"}),
            "the provider did not accept the key",
        ),
        (lambda body: (403, {"error": "forbidden"}), "the provider did not accept the key"),
        (
            lambda body: (429, {"error": "slow down"}),
            "the provider is limiting requests for this key; try again in a minute",
        ),
        (
            lambda body: (500, {"error": f"upstream {FAKE_KEY}"}),
            "the provider could not read the page (HTTP 500); try again later",
        ),
        (
            lambda body: (200, completion("   ")),
            "the vision model sent back no text for this page",
        ),
        (
            lambda body: (None, b"garbage"),
            "the vision model sent back no text for this page",
        ),
    ],
    ids=["401", "403", "429", "500", "empty", "garbage"],
)
def test_a_provider_error_maps_to_plain_wording(
    marked,
    store,
    fake_provider,  # noqa: F811
    caplog,
    reply,
    message,
):
    fake_provider.reply = reply

    response = read_with_vision(marked)

    assert response.status_code == 502
    error = response.json()["error"]
    assert (error["code"], error["message"]) == ("vision_failed", message)
    assert FAKE_KEY not in response.text and FAKE_KEY not in caplog.text
    page = client.get(f"/v1/jobs/{marked}").json()["result"]["pages"][0]
    assert (page["method"], page["needs_vision"]) == ("tesseract", True)  # may be tried again
    assert store.get(marked).data == SCANNED


def test_a_provider_timeout_maps_to_plain_wording(marked, fake_provider, monkeypatch):  # noqa: F811
    def too_slow(*args, **kwargs):
        raise vision.VisionError("timeout", "the vision model did not answer within 60 seconds")

    monkeypatch.setattr(vision, "read_image", too_slow)

    response = read_with_vision(marked)

    assert response.status_code == 502
    assert (
        response.json()["error"]["message"] == "the vision model took too long to answer; try again"
    )


def test_an_unreachable_provider_maps_to_plain_wording(marked, monkeypatch):
    def unreachable(*args, **kwargs):
        raise vision.VisionError("unreachable", "the vision model could not be reached")

    monkeypatch.setattr(vision, "read_image", unreachable)

    response = read_with_vision(marked)

    assert response.status_code == 502
    assert (
        response.json()["error"]["message"] == "the provider could not be reached; try again later"
    )


# --- the spec describes what the route returns ----------------------------------------

ROUTE = "/v1/jobs/{job_id}/pages/{number}/vision"


def test_the_page_from_the_route_and_a_marked_job_match_their_schemas(
    marked,
    fake_provider,  # noqa: F811
):
    before = client.get(f"/v1/jobs/{marked}").json()
    check(before, schema("/v1/jobs/{job_id}", "get", 200, "application/json"))
    assert before["result"]["pages"][0]["needs_vision"] is True

    page = read_with_vision(marked).json()
    found = schema(ROUTE, "post", 200, "application/json")
    check(page, found)
    assert set(resolve(found)["properties"]) == set(page)
    assert set(resolve(found)["required"]) == set(page)


@pytest.mark.parametrize(
    ("status", "headers", "number"),
    [
        (400, {"X-Model-Preset": "nope", "X-Model-Key": FAKE_KEY}, SCAN),
        (401, {}, SCAN),
        (404, KEY, 9),
        (409, KEY, READ_WELL),
    ],
)
def test_the_errors_from_the_route_match_their_schema(marked, status, headers, number):
    response = read_with_vision(marked, number=number, headers=headers)

    assert response.status_code == status
    check(response.json(), schema(ROUTE, "post", status, "application/json"))


def test_a_provider_error_matches_its_schema(marked, fake_provider):  # noqa: F811
    fake_provider.reply = lambda body: (500, {})
    response = read_with_vision(marked)

    assert response.status_code == 502
    check(response.json(), schema(ROUTE, "post", 502, "application/json"))


def test_a_failed_job_has_no_page_to_read(store):
    job = store.submit(SCANNED, lambda data, job: 1 / 0, format="json")
    assert job.finished.wait(WAIT)

    response = read_with_vision(job.id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "vision_not_needed"


def test_an_unexpected_error_while_reading_is_a_plain_502(marked, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError(f"internal detail {FAKE_KEY}")

    monkeypatch.setattr(vision, "read_image", broken)

    response = read_with_vision(marked)

    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "vision_failed",
        "message": "the vision model could not read the page; try again later",
        "request_id": response.headers["x-request-id"],
    }
    assert FAKE_KEY not in caplog.text
    assert read_with_vision(marked).status_code == 502  # the page was let go: not page_busy
