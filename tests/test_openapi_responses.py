"""The spec at `/openapi.json` describes the bodies `POST /v1/extract`, `GET /v1/jobs/{id}` and
`GET /v1/capabilities` really return: each live response below, read from a fixture PDF, is
checked against the schema the spec gives for it, and the schema names exactly the keys the
response has.
"""

from pathlib import Path

import jsonschema
import pytest
from fastapi.testclient import TestClient

import llm_pdf_ocr.app as api
from llm_pdf_ocr import extract as extract_module
from llm_pdf_ocr.app import app
from llm_pdf_ocr.config import Limits
from llm_pdf_ocr.jobs import JobStore
from llm_pdf_ocr.ocr import OcrError

BORN_DIGITAL = (Path(__file__).parent / "fixtures" / "born_digital.pdf").read_bytes()
WAIT = 5

client = TestClient(app)
SPEC = client.get("/openapi.json").json()


def schema(path: str, method: str, status: int, media_type: str) -> dict:
    """The schema the spec gives for one response, with its `$ref`s still resolvable."""
    responses = SPEC["paths"][path][method]["responses"]
    assert str(status) in responses, f"{method.upper()} {path} documents no {status}"
    content = responses[str(status)].get("content", {})
    assert media_type in content, f"{method.upper()} {path} {status} has no {media_type}"
    assert "schema" in content[media_type], f"{method.upper()} {path} {status} has no schema"
    return {**content[media_type]["schema"], "components": SPEC.get("components", {})}


def resolve(found: dict) -> dict:
    """Follow a top-level `$ref` into the spec's components."""
    while "$ref" in found:
        found = SPEC["components"]["schemas"][found["$ref"].rsplit("/", 1)[1]]
    return found


def check(body, against: dict) -> None:
    jsonschema.Draft202012Validator(against).validate(body)


def extract(**form):
    return client.post(
        "/v1/extract", files={"file": ("doc.pdf", BORN_DIGITAL, "application/pdf")}, data=form
    )


@pytest.fixture
def store():
    saved = app.state.jobs
    app.state.jobs = JobStore(max_jobs=3, ttl=60)
    yield app.state.jobs
    app.state.jobs.shutdown()
    app.state.jobs = saved


def finished_job(store: JobStore, **form) -> dict:
    response = client.post(
        "/v1/jobs", files={"file": ("doc.pdf", BORN_DIGITAL, "application/pdf")}, data=form
    )
    assert response.status_code == 202
    job_id = response.json()["id"]
    assert store.get(job_id).finished.wait(WAIT)
    got = client.get(f"/v1/jobs/{job_id}")
    assert got.status_code == 200
    return got.json()


# POST /v1/extract


def test_the_json_document_from_extract_matches_its_schema():
    response = extract(format="json", method="text_layer")
    assert response.status_code == 200
    body = response.json()
    found = schema("/v1/extract", "post", 200, "application/json")

    check(body, found)
    document = resolve(found)
    assert set(document["properties"]) == set(body)
    page = resolve(document["properties"]["pages"]["items"])
    assert set(page["properties"]) == set(body["pages"][0])
    assert set(document["required"]) == set(body)
    assert set(page["required"]) == set(body["pages"][0])


def test_a_failed_page_matches_its_schema(monkeypatch):
    def fail(page, language, timeout):
        raise OcrError("tesseract could not read the page")

    monkeypatch.setattr(extract_module, "ocr_page", fail)
    body = extract(format="json", method="tesseract").json()

    assert body["pages"][0]["status"] == "failed"
    assert body["pages"][0]["error"] is not None
    check(body, schema("/v1/extract", "post", 200, "application/json"))


def test_the_document_schema_refuses_a_key_the_api_does_not_send():
    body = extract(format="json", method="text_layer").json()
    found = schema("/v1/extract", "post", 200, "application/json")
    body["pages"][0]["number"] = 1

    with pytest.raises(jsonschema.ValidationError):
        check(body, found)


def test_the_markdown_from_extract_matches_its_schema():
    response = extract(method="text_layer")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")

    check(response.text, schema("/v1/extract", "post", 200, "text/markdown"))


@pytest.mark.parametrize(
    ("status", "send"),
    [
        (400, lambda: extract(pages="99")),
        (400, lambda: client.post("/v1/extract", files={"file": ("x.pdf", b"not a pdf")})),
        (413, lambda: extract()),
        (422, lambda: client.post("/v1/extract")),
    ],
)
def test_the_errors_from_extract_match_their_schema(status, send):
    saved = app.state.limits
    if status == 413:
        app.state.limits = Limits(max_pages=0)
    try:
        response = send()
    finally:
        app.state.limits = saved
    assert response.status_code == status

    found = schema("/v1/extract", "post", status, "application/json")
    check(response.json(), found)
    assert set(resolve(found)["properties"]) == {"error"}


# GET /v1/jobs/{id}


def test_a_done_json_job_matches_its_schema(store):
    body = finished_job(store, format="json", method="text_layer")
    found = schema("/v1/jobs/{job_id}", "get", 200, "application/json")

    assert body["status"] == "done"
    check(body, found)
    assert set(resolve(found)["properties"]) == set(body)
    assert set(resolve(found)["required"]) == set(body)
    # the result is the very document `/v1/extract` documents
    check(body["result"], schema("/v1/extract", "post", 200, "application/json"))


def test_a_done_markdown_job_matches_its_schema(store):
    body = finished_job(store, method="text_layer")

    assert isinstance(body["result"], str)
    check(body, schema("/v1/jobs/{job_id}", "get", 200, "application/json"))


def test_a_failed_job_matches_its_schema(store, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "extract_pdf", explode)
    body = finished_job(store, method="text_layer")

    assert body["status"] == "failed"
    check(body, schema("/v1/jobs/{job_id}", "get", 200, "application/json"))


def test_the_job_schema_refuses_a_status_the_api_does_not_send(store):
    body = finished_job(store, method="text_layer")
    body["status"] = "finished"

    with pytest.raises(jsonschema.ValidationError):
        check(body, schema("/v1/jobs/{job_id}", "get", 200, "application/json"))


def test_an_unknown_job_matches_its_error_schema():
    response = client.get("/v1/jobs/no-such-job")
    assert response.status_code == 404

    check(response.json(), schema("/v1/jobs/{job_id}", "get", 404, "application/json"))


# GET /v1/capabilities


def test_capabilities_match_their_schema():
    body = client.get("/v1/capabilities").json()
    found = schema("/v1/capabilities", "get", 200, "application/json")

    check(body, found)
    capabilities = resolve(found)
    assert set(capabilities["properties"]) == set(body)
    assert set(capabilities["required"]) == set(body)
    preset = resolve(capabilities["properties"]["presets"]["items"])
    assert set(preset["properties"]) == set(body["presets"][0]) == {"id", "label", "vision_model"}
