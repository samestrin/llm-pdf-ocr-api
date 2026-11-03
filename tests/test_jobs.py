"""Jobs: `POST /v1/jobs` returns an id, `GET /v1/jobs/{id}` gives progress and the result.

Jobs are held in memory. Once a job ends it holds no PDF bytes, its result is
dropped after a TTL, and over a maximum number of held jobs `POST` answers 429.
"""

import json
import re
import threading
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from fastapi.testclient import TestClient

import llm_pdf_ocr.app as api
from llm_pdf_ocr import extract, jobs, vision
from llm_pdf_ocr import ocr as ocr_module
from llm_pdf_ocr.app import app
from llm_pdf_ocr.config import ConfigError, JobSettings, VisionConfig, load_job_settings
from llm_pdf_ocr.extract import MethodChoice, extract_pdf
from llm_pdf_ocr.jobs import JobStore, TooManyJobsError

ROOT = Path(__file__).parent.parent
MIXED = (Path(__file__).parent / "fixtures" / "mixed.pdf").read_bytes()
WAIT = 5

client = TestClient(app)


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
    """A fresh job store for the app, with a clock the test moves."""
    saved = app.state.jobs
    app.state.jobs = JobStore(max_jobs=3, ttl=60, clock=clock)
    yield app.state.jobs
    app.state.jobs.shutdown()
    app.state.jobs = saved


def text_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        doc.new_page().insert_text((72, 72), f"Text layer of page {n}, long enough to be usable.")
    data = doc.tobytes()
    doc.close()
    return data


def submit(data: bytes = MIXED, **form):
    return client.post("/v1/jobs", files={"file": ("doc.pdf", data, "application/pdf")}, data=form)


def finished(store: JobStore, job_id: str):
    job = store.get(job_id)
    assert job.finished.wait(WAIT)
    return job


class Gate:
    """Hold the text-layer read of each page until the test lets it through."""

    def __init__(self, monkeypatch):
        self.entered = threading.Semaphore(0)
        self.release = threading.Semaphore(0)
        real = extract.read_page

        def read_page(page):
            self.entered.release()
            assert self.release.acquire(timeout=WAIT)
            return real(page)

        monkeypatch.setattr(extract, "read_page", read_page)

    def let_one_page_through(self):
        assert self.entered.acquire(timeout=WAIT)
        self.release.release()


# Settings


def test_job_settings_defaults_when_unset():
    assert load_job_settings({}) == JobSettings(ttl=3600.0, max_jobs=20)


def test_job_settings_are_read_from_the_environment():
    settings = load_job_settings({"OCR_JOB_TTL_SECONDS": "90.5", "OCR_MAX_JOBS": "4"})
    assert settings == JobSettings(ttl=90.5, max_jobs=4)


@pytest.mark.parametrize(
    "name,value",
    [
        ("OCR_JOB_TTL_SECONDS", "soon"),
        ("OCR_JOB_TTL_SECONDS", "0"),
        ("OCR_MAX_JOBS", "2.5"),
        ("OCR_MAX_JOBS", "-1"),
    ],
)
def test_bad_job_settings_raise(name, value):
    with pytest.raises(ConfigError, match=name):
        load_job_settings({name: value})


def test_the_app_holds_jobs_with_the_settings_from_the_environment():
    settings = load_job_settings()
    assert app.state.jobs.max_jobs == settings.max_jobs
    assert app.state.jobs.ttl == settings.ttl


def test_the_dockerfile_runs_a_single_uvicorn_worker():
    """In-memory jobs are only visible to the process that holds them."""
    lines = (ROOT / "Dockerfile").read_text().splitlines()
    cmd = next(line for line in lines if line.startswith("CMD "))
    assert "uvicorn" in cmd
    assert "--workers" not in cmd


# The store


def work_that_reads(pages: int, result="the result"):
    def work(data: bytes, job) -> str:
        job.selected(list(range(1, pages + 1)))
        for n in range(1, pages + 1):
            job.page_done(n, "ok")
        return result

    return work


def test_job_ids_are_random_url_safe_tokens(monkeypatch, clock):
    calls = []

    def token(nbytes=None):
        calls.append(nbytes)
        return f"token-{len(calls)}"

    monkeypatch.setattr(jobs.secrets, "token_urlsafe", token)
    store = JobStore(max_jobs=3, ttl=60, clock=clock)
    ids = [store.submit(b"%PDF", work_that_reads(1)).id for _ in range(2)]
    store.shutdown()
    assert ids == ["token-1", "token-2"]
    assert calls and all(n is None or n >= 16 for n in calls)


def test_real_job_ids_are_long_and_distinct(clock):
    store = JobStore(max_jobs=3, ttl=60, clock=clock)
    ids = {store.submit(b"%PDF", work_that_reads(1)).id for _ in range(3)}
    store.shutdown()
    assert len(ids) == 3
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{22,}", i) for i in ids)


def test_a_done_job_holds_its_result_and_no_pdf_bytes(clock):
    store = JobStore(max_jobs=3, ttl=60, clock=clock)
    job = store.submit(b"%PDF bytes", work_that_reads(2))
    assert job.finished.wait(WAIT)
    store.shutdown()
    assert job.status == "done"
    assert job.result == "the result"
    assert job.data is None


def test_a_failed_job_holds_no_pdf_bytes_and_a_generic_error(clock):
    def work(data, job):
        raise RuntimeError("secret detail from deep inside")

    store = JobStore(max_jobs=3, ttl=60, clock=clock)
    job = store.submit(b"%PDF bytes", work)
    assert job.finished.wait(WAIT)
    store.shutdown()
    assert job.status == "failed"
    assert job.data is None
    assert job.result is None
    assert job.error == {"code": "job_failed", "message": "the job could not be completed"}


def test_the_store_refuses_a_job_over_the_cap(clock):
    store = JobStore(max_jobs=2, ttl=60, clock=clock)
    for _ in range(2):
        store.submit(b"%PDF", work_that_reads(1))
    with pytest.raises(TooManyJobsError):
        store.submit(b"%PDF", work_that_reads(1))
    store.shutdown()


def test_an_expired_job_is_dropped_and_frees_its_place(clock):
    store = JobStore(max_jobs=1, ttl=60, clock=clock)
    job = store.submit(b"%PDF", work_that_reads(1))
    assert job.finished.wait(WAIT)
    clock.now += 59
    assert store.get(job.id) is job
    clock.now += 1
    assert store.get(job.id) is None
    assert job.result is None
    store.submit(b"%PDF", work_that_reads(1))  # the cap has room again
    store.shutdown()


def test_a_job_still_running_does_not_expire(clock):
    started, release = threading.Event(), threading.Event()

    def work(data, job):
        started.set()
        assert release.wait(WAIT)
        return "late"

    store = JobStore(max_jobs=1, ttl=60, clock=clock)
    job = store.submit(b"%PDF", work)
    assert started.wait(WAIT)
    clock.now += 3600
    assert store.get(job.id) is job
    release.set()
    assert job.finished.wait(WAIT)
    store.shutdown()
    assert job.status == "done"


# Progress from extract_pdf


class Recorder:
    def __init__(self):
        self.events = []

    def selected(self, numbers):
        self.events.append(("selected", list(numbers)))

    def page_done(self, number, status):
        self.events.append((number, status))


def test_vision_pages_report_progress_too(monkeypatch):
    config = VisionConfig(
        base_url="http://vision.invalid/v1", default_model="m", allowed_models=("m",)
    )
    real = vision.render_page

    def render(page):
        if page.number == 1:  # 0-based: the PDF's page 2
            raise RuntimeError("cannot render")
        return real(page)

    def read(png, number, config, model=None):
        return vision.VisionResult(number=number, text="vision text", model="m")

    monkeypatch.setattr(vision, "render_page", render)
    monkeypatch.setattr(vision, "read_image", read)
    progress = Recorder()

    extract_pdf(text_pdf(3), method=MethodChoice.VISION, vision=config, progress=progress)

    assert progress.events[0] == ("selected", [1, 2, 3])
    assert sorted(progress.events[1:]) == [(1, "ok"), (2, "failed"), (3, "ok")]


@pytest.mark.parametrize("tesseract_works, status", [(True, "ok"), (False, "failed")])
def test_a_page_that_falls_back_to_tesseract_reports_progress_once(
    monkeypatch, tesseract_works, status
):
    config = VisionConfig(
        base_url="http://vision.invalid/v1",
        default_model="m",
        allowed_models=("m",),
        fallback_to_tesseract=True,
    )

    def read(png, number, config, model=None):
        if number == 2:
            raise vision.VisionError("the vision model timed out")
        return vision.VisionResult(number=number, text="vision text", model="m")

    def ocr(page, language, timeout):
        if not tesseract_works:
            raise ocr_module.OcrError("tesseract failed")
        return ocr_module.OcrResult(number=page.number + 1, text="tesseract text", confidence=0.9)

    monkeypatch.setattr(vision, "read_image", read)
    monkeypatch.setattr(extract, "ocr_page", ocr)
    progress = Recorder()

    doc = extract_pdf(text_pdf(3), method=MethodChoice.VISION, vision=config, progress=progress)

    assert doc.pages[1].fallback is not None
    assert doc.pages[1].status == status
    assert sorted(progress.events[1:]) == [(1, "ok"), (2, status), (3, "ok")]


# The API


def test_post_returns_an_id_and_the_job_ends_with_the_result(store):
    response = submit(text_pdf(2), format="json")
    assert response.status_code == 202
    body = response.json()
    assert response.headers["location"] == f"/v1/jobs/{body['id']}"
    finished(store, body["id"])

    got = client.get(f"/v1/jobs/{body['id']}")
    assert got.status_code == 200
    job = got.json()
    assert job["id"] == body["id"]
    assert job["status"] == "done"
    assert job["pages_total"] == 2
    assert job["pages_done"] == 2
    assert job["pages"] == [{"page": 1, "status": "ok"}, {"page": 2, "status": "ok"}]
    assert job["error"] is None
    assert job["result"]["page_count"] == 2
    assert "page 2" in job["result"]["pages"][1]["text"]


def test_a_markdown_job_gives_the_markdown_text(store):
    job_id = submit(text_pdf(1)).json()["id"]
    finished(store, job_id)
    result = client.get(f"/v1/jobs/{job_id}").json()["result"]
    assert result.startswith("## Page 1\n\nMethod: text_layer")


def test_a_multi_page_job_reports_progress_per_page(store, monkeypatch):
    gate = Gate(monkeypatch)
    job_id = submit(text_pdf(3), format="json", pages="1-3").json()["id"]

    gate.let_one_page_through()
    assert gate.entered.acquire(timeout=WAIT)  # page 2 has started, so page 1 is recorded
    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["status"] == "running"
    assert job["pages_total"] == 3
    assert job["pages_done"] == 1
    assert job["pages"] == [
        {"page": 1, "status": "ok"},
        {"page": 2, "status": "pending"},
        {"page": 3, "status": "pending"},
    ]
    assert job["result"] is None

    gate.release.release()
    gate.let_one_page_through()
    finished(store, job_id)
    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["status"] == "done"
    assert job["pages_done"] == 3


def test_progress_counts_only_the_selected_pages(store):
    job_id = submit(text_pdf(5), format="json", pages="2,4").json()["id"]
    finished(store, job_id)
    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["pages"] == [{"page": 2, "status": "ok"}, {"page": 4, "status": "ok"}]


def test_a_failed_page_is_reported_in_the_progress(store, monkeypatch):
    def broken(page):
        raise RuntimeError("bad page")

    monkeypatch.setattr(extract, "read_page", broken)
    job_id = submit(text_pdf(1), format="json", method="text_layer").json()["id"]
    finished(store, job_id)
    job = client.get(f"/v1/jobs/{job_id}").json()
    assert job["status"] == "done"
    assert job["pages"] == [{"page": 1, "status": "failed"}]
    assert job["result"]["failed_pages"] == [1]


def test_a_queued_job_has_no_progress_yet(store, monkeypatch):
    gate = Gate(monkeypatch)
    first = submit(text_pdf(1)).json()["id"]
    assert gate.entered.acquire(timeout=WAIT)
    second = submit(text_pdf(1)).json()["id"]
    job = client.get(f"/v1/jobs/{second}").json()
    assert job["status"] == "queued"
    assert job["pages_total"] is None
    assert job["pages_done"] == 0
    assert job["pages"] == []
    gate.release.release()
    gate.let_one_page_through()
    finished(store, first)
    finished(store, second)


def test_after_a_job_ends_it_holds_no_pdf_bytes(store):
    job_id = submit(text_pdf(2)).json()["id"]
    job = finished(store, job_id)
    assert job.status == "done"
    assert job.data is None


def test_a_job_that_fails_holds_no_pdf_bytes_and_says_why(store, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("internal detail")

    monkeypatch.setattr(api, "extract_pdf", explode)
    job_id = submit(text_pdf(1)).json()["id"]
    job = finished(store, job_id)
    assert job.data is None
    body = client.get(f"/v1/jobs/{job_id}").json()
    assert body["status"] == "failed"
    assert body["result"] is None
    assert body["error"] == {"code": "job_failed", "message": "the job could not be completed"}
    assert "internal detail" not in json.dumps(body)


def test_an_expired_jobs_result_is_gone(store, clock):
    job_id = submit(text_pdf(1)).json()["id"]
    job = finished(store, job_id)
    clock.now += store.ttl
    response = client.get(f"/v1/jobs/{job_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "job_not_found"
    assert job.result is None
    assert job.data is None


def test_an_unknown_job_is_404():
    response = client.get("/v1/jobs/no-such-job")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "job_not_found", "message": "no job with this id, or it has expired"}
    }


def test_the_job_cap_gives_429(store, monkeypatch):
    gate = Gate(monkeypatch)
    ids = [submit(text_pdf(1)).json()["id"] for _ in range(store.max_jobs)]
    response = submit(text_pdf(1))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_jobs"
    for _ in ids:
        gate.let_one_page_through()
    for job_id in ids:
        finished(store, job_id)


def test_finished_jobs_count_until_they_expire(store, clock):
    ids = [submit(text_pdf(1)).json()["id"] for _ in range(store.max_jobs)]
    for job_id in ids:
        finished(store, job_id)
    assert submit(text_pdf(1)).status_code == 429
    clock.now += store.ttl
    response = submit(text_pdf(1))
    assert response.status_code == 202
    finished(store, response.json()["id"])


@pytest.mark.parametrize(
    "data,form,status,code",
    [
        (b"not a pdf", {}, 400, "invalid_pdf"),
        (text_pdf(2), {"pages": "3"}, 400, "page_out_of_range"),
        (text_pdf(2), {"model": "any-model"}, 400, "model_not_allowed"),
        (text_pdf(2), {"method": "vision"}, 400, "vision_not_configured"),
    ],
)
def test_a_bad_request_is_refused_before_a_job_is_made(store, data, form, status, code):
    response = submit(data, **form)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert len(store) == 0


def test_the_request_limits_apply_before_a_job_is_made(store):
    saved = app.state.limits
    app.state.limits = replace(saved, max_pages=2, max_file_bytes=len(text_pdf(3)) + 10)
    try:
        assert submit(text_pdf(3)).json()["error"]["code"] == "too_many_pages"
        big = submit(text_pdf(3) + b"\0" * 20)
        assert (big.status_code, big.json()["error"]["code"]) == (413, "file_too_large")
    finally:
        app.state.limits = saved
    assert len(store) == 0
