"""Presets and per-request credentials: the allowlist, and a visitor's key kept out of everything.

A fake key is planted in the request headers and followed through a successful read and
every failure path; it must never reach a log line, an error body, a job result or
`/v1/capabilities`.
"""

import json
import logging
import time

import pymupdf
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_vision import FakeServer, completion

from llm_pdf_ocr import presets as presets_module
from llm_pdf_ocr.app import (
    RequestVision,
    app,
    get_public_demo_config,
    get_vision_config,
)
from llm_pdf_ocr.config import (
    ConfigError,
    PublicDemoConfig,
    VisionConfig,
    load_public_demo_config,
)
from llm_pdf_ocr.jobs import JobStore
from llm_pdf_ocr.presets import (
    CredentialsError,
    Preset,
    load_presets,
    public_presets,
    request_vision_config,
)
from llm_pdf_ocr.requestlog import RequestLog
from llm_pdf_ocr.vision import VisionError, read_image

FAKE_KEY = "sk-visitor-fake-key-PLANTED-7f3a9c"
DEMO_CODE = "sk-demo-PLANTEDDEMOCODE234567"
GATEWAY = "http://gateway.internal:4000/v1"
SERVER = VisionConfig(timeout=7.0, concurrency=3)

DEMO_HEADERS = ({"X-Demo-Code": DEMO_CODE}, {"X-Model-Preset": "demo", "X-Demo-Code": DEMO_CODE})

TABLE = {
    "openai": ("OpenAI", "https://api.openai.com/v1", "gpt-5-nano"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "qwen/qwen3.7-flash"),
    "synthetic": ("Synthetic", "https://api.synthetic.new/openai/v1", "syn:small:vision"),
    "alibaba": (
        "Alibaba (DashScope intl)",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "qwen-vl-plus",
    ),
}


NO_DEMO = PublicDemoConfig()


def _config(headers, demo=NO_DEMO, server=SERVER):
    return request_vision_config(headers, server, demo)


# --- presets.json ---------------------------------------------------------------------


def test_the_preset_file_holds_the_plan_table_and_demo():
    by_id = {p.id: p for p in load_presets()}

    assert set(by_id) == set(TABLE) | {"demo"}
    for preset_id, (label, base_url, model) in TABLE.items():
        assert (by_id[preset_id].label, by_id[preset_id].base_url) == (label, base_url)
        assert by_id[preset_id].vision_model == model
        assert by_id[preset_id].base_url_env is None
    assert by_id["demo"].base_url is None
    assert by_id["demo"].base_url_env == "DEMO_GATEWAY_URL"


def test_the_preset_file_ships_inside_the_package():
    assert presets_module.PRESETS_FILE.parent.name == "llm_pdf_ocr"
    assert presets_module.PRESETS_FILE.is_file()


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([{"id": "a", "label": "A", "vision_model": "m"}], "needs a base_url"),
        (
            [{"id": "a", "label": "A", "vision_model": "m", "base_url": "http://x.example/v1"}],
            "https",
        ),
        (
            [
                {"id": "a", "label": "A", "vision_model": "m", "base_url": "https://x/v1"},
                {"id": "a", "label": "B", "vision_model": "m", "base_url": "https://y/v1"},
            ],
            "twice",
        ),
        ([{"id": "A/b", "label": "A", "vision_model": "m", "base_url": "https://x/v1"}], "id"),
        ([{"id": "a", "label": "A", "base_url": "https://x/v1"}], "vision_model"),
        ([{"id": "a", "label": "A", "vision_model": "m", "base_url_env": "HOME"}], "base_url_env"),
    ],
    ids=["no-url", "not-https", "duplicate", "bad-id", "no-model", "other-env"],
)
def test_a_bad_preset_file_is_refused(tmp_path, entries, message):
    path = tmp_path / "presets.json"
    path.write_text(json.dumps(entries))

    with pytest.raises(ValueError, match=message):
        load_presets(path)


def test_public_presets_carry_id_label_and_model_only():
    listed = public_presets()

    assert [p["id"] for p in listed] == [p.id for p in load_presets()]
    for entry in listed:
        assert set(entry) == {"id", "label", "vision_model"}
    assert "http" not in json.dumps(listed)


# --- load_public_demo_config ----------------------------------------------------------


def test_public_demo_config_is_off_by_default():
    config = load_public_demo_config({})

    assert config == PublicDemoConfig()
    assert not config.demo_available
    assert config.notice is None


def test_public_demo_config_reads_the_gateway_and_notice():
    config = load_public_demo_config(
        {"DEMO_GATEWAY_URL": " http://gateway.internal:4000/v1/ ", "PUBLIC_DEMO_NOTICE": " Hi. "}
    )

    assert config.gateway_url == "http://gateway.internal:4000/v1"
    assert config.demo_available
    assert config.notice == "Hi."


@pytest.mark.parametrize("value", ["gateway.internal:4000", "ftp://gateway/v1", "http://"])
def test_a_gateway_that_is_not_an_http_url_is_a_config_error(value):
    with pytest.raises(ConfigError, match="DEMO_GATEWAY_URL") as info:
        load_public_demo_config({"DEMO_GATEWAY_URL": value})
    assert value not in str(info.value)  # a URL may carry a user and password


# --- request_vision_config ------------------------------------------------------------


def test_no_credentials_keeps_the_servers_own_config():
    assert _config({}) is SERVER
    assert not _config({}).enabled


@pytest.mark.parametrize("preset_id", sorted(TABLE))
def test_a_preset_and_key_build_a_config_for_that_preset_only(preset_id):
    config = _config({"X-Model-Preset": preset_id, "X-Model-Key": FAKE_KEY})

    label, base_url, model = TABLE[preset_id]
    assert config.base_url == base_url
    assert config.api_key == FAKE_KEY
    assert config.default_model == model
    assert config.allowed_models == (model,)
    assert (config.timeout, config.concurrency) == (SERVER.timeout, SERVER.concurrency)
    assert not config.fallback_to_tesseract


def test_the_preset_id_is_matched_without_case_or_spaces():
    config = _config({"X-Model-Preset": " OpenAI ", "X-Model-Key": FAKE_KEY})

    assert config.base_url == TABLE["openai"][1]


def test_a_demo_code_reaches_the_gateway():
    demo = PublicDemoConfig(gateway_url=GATEWAY)

    model = next(p for p in load_presets() if p.id == "demo").vision_model

    for headers in DEMO_HEADERS:
        config = _config(headers, demo)
        assert config.base_url == GATEWAY
        assert config.api_key == DEMO_CODE
        assert config.default_model == model


def _refused(headers, demo=NO_DEMO) -> CredentialsError:
    with pytest.raises(CredentialsError) as info:
        _config(headers, demo)
    return info.value


def test_an_unknown_preset_is_400_preset_not_allowed():
    error = _refused({"X-Model-Preset": "mistral", "X-Model-Key": FAKE_KEY})

    assert (error.status_code, error.code) == (400, "preset_not_allowed")


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example/v1",
        "http://169.254.169.254/latest",
        "//internal:4000",
        "api.openai.com",
        "openai/../demo",
        "http://api.openai.com/v1",
        TABLE["openai"][1],  # even an allowed base URL: only ids are read
    ],
)
def test_a_url_like_preset_value_is_refused(value):
    error = _refused({"X-Model-Preset": value, "X-Model-Key": FAKE_KEY})

    assert (error.status_code, error.code) == (400, "preset_not_allowed")
    assert value not in str(error)


def test_the_demo_preset_is_refused_when_no_gateway_is_set():
    for headers in DEMO_HEADERS:
        error = _refused(headers)
        assert (error.status_code, error.code) == (400, "preset_not_allowed")


def test_a_key_without_a_preset_is_refused():
    error = _refused({"X-Model-Key": FAKE_KEY})

    assert (error.status_code, error.code) == (400, "preset_not_allowed")


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Model-Preset": "openai"},
        {"X-Model-Preset": "openai", "X-Model-Key": "   "},
        {"X-Model-Preset": "openai", "X-Demo-Code": DEMO_CODE},  # a code is not a key
        {"X-Model-Preset": "demo", "X-Model-Key": FAKE_KEY},  # nor a key a code
    ],
    ids=["no-key", "blank-key", "code-for-preset", "key-for-demo"],
)
def test_a_preset_without_its_credential_is_401_credentials_required(headers):
    error = _refused(headers, PublicDemoConfig(gateway_url=GATEWAY))

    assert (error.status_code, error.code) == (401, "credentials_required")


@pytest.mark.parametrize(
    "key",
    [FAKE_KEY + " extra", FAKE_KEY + "\x7f", "é" + FAKE_KEY, FAKE_KEY + "x" * 600],
    ids=["space", "control", "non-ascii", "too-long"],
)
def test_a_malformed_key_is_400_invalid_key_and_not_echoed(key):
    error = _refused({"X-Model-Preset": "openai", "X-Model-Key": key})

    assert (error.status_code, error.code) == (400, "invalid_key")
    assert FAKE_KEY not in str(error) and FAKE_KEY not in repr(error)


def test_the_config_repr_never_shows_the_key():
    config = _config({"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY})

    assert FAKE_KEY not in repr(config)
    assert FAKE_KEY not in str(config)


# --- the key in use, and in every failure path ----------------------------------------


@pytest.fixture
def fake_provider(monkeypatch):
    """A fake OpenAI-compatible endpoint standing in for the `openai` preset's base URL."""
    server = FakeServer()
    server.thread.start()
    real = load_presets()
    patched = tuple(
        Preset(p.id, p.label, p.vision_model, server.base_url, None) if p.id == "openai" else p
        for p in real
    )
    monkeypatch.setattr(presets_module, "load_presets", lambda path=None: patched)
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


def _png() -> bytes:
    with pymupdf.open() as doc:
        doc.new_page(width=200, height=200).insert_text((20, 50), "scan")
        return doc[0].get_pixmap(dpi=40).tobytes("png")


def test_the_key_is_sent_to_the_preset_for_this_request(fake_provider, caplog):
    caplog.set_level(logging.DEBUG)
    config = _config({"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY})

    result = read_image(_png(), 1, config)

    (path, headers, body) = fake_provider.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {FAKE_KEY}"
    assert body["model"] == TABLE["openai"][2]
    assert result.text == "# Page"
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize(
    "reply",
    [
        lambda body: (401, {"error": {"message": f"Incorrect API key provided: {FAKE_KEY}"}}),
        lambda body: (429, {"error": f"rate limit for key {FAKE_KEY}"}),
        lambda body: (500, {"detail": f"upstream with {FAKE_KEY}"}),
        lambda body: (200, {"choices": [], "echo": FAKE_KEY}),
        lambda body: (200, completion("   ")),
        lambda body: (None, f"garbage {FAKE_KEY}".encode()),
        lambda body: (302, {}, {"Location": f"http://elsewhere.example/?k={FAKE_KEY}"}),
    ],
    ids=["401", "429", "500", "bad-reply", "empty", "not-http", "redirect"],
)
def test_the_key_never_appears_in_an_error_or_a_log_line(fake_provider, caplog, reply):
    caplog.set_level(logging.DEBUG)
    fake_provider.reply = reply
    config = _config({"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY})

    with pytest.raises(VisionError) as info:
        read_image(_png(), 1, config)

    for text in (str(info.value), repr(info.value), caplog.text):
        assert FAKE_KEY not in text


def test_the_key_never_appears_when_the_provider_is_unreachable(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    unreachable = tuple(
        Preset(p.id, p.label, p.vision_model, "http://127.0.0.1:9/v1", None)
        if p.id == "openai"
        else p
        for p in load_presets()
    )
    monkeypatch.setattr(presets_module, "load_presets", lambda path=None: unreachable)
    config = _config({"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY})

    with pytest.raises(VisionError) as info:
        read_image(_png(), 1, config)

    assert info.value.code == "unreachable"
    for text in (str(info.value), repr(info.value), caplog.text):
        assert FAKE_KEY not in text


# --- over HTTP: the dependency, and the routes that must ignore the headers -----------


def _probe_app() -> TestClient:
    """A route using `RequestVision`, as T2's page route will, with the app's error handlers."""
    probe = FastAPI(exception_handlers=app.exception_handlers)
    probe.add_middleware(RequestLog)
    probe.dependency_overrides = app.dependency_overrides

    @probe.post("/probe")
    def read(vision: RequestVision) -> dict:
        return {"text": read_image(_png(), 1, vision).text}

    return TestClient(probe, raise_server_exceptions=False)


@pytest.fixture
def demo_on():
    app.dependency_overrides[get_public_demo_config] = lambda: PublicDemoConfig(
        gateway_url=GATEWAY, notice="Public demo: do not upload private files."
    )


ALL_FAILURES = [
    {"X-Model-Preset": "mistral", "X-Model-Key": FAKE_KEY},
    {"X-Model-Preset": "https://evil.example/v1", "X-Model-Key": FAKE_KEY},
    {"X-Model-Key": FAKE_KEY},
    {"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY + " x"},
    {"X-Model-Preset": "openai", "X-Demo-Code": FAKE_KEY},
    {"X-Demo-Code": FAKE_KEY},  # no gateway set
]


@pytest.mark.parametrize("headers", ALL_FAILURES)
def test_a_refused_request_answers_with_the_error_body_and_no_key(headers, caplog):
    caplog.set_level(logging.DEBUG)

    response = _probe_app().post("/probe", headers=headers)

    assert response.status_code in (400, 401)
    error = response.json()["error"]
    assert error["code"] in {"preset_not_allowed", "credentials_required", "invalid_key"}
    assert error["request_id"] == response.headers["x-request-id"]
    assert FAKE_KEY not in response.text
    assert FAKE_KEY not in caplog.text


@pytest.mark.parametrize("status", [401, 429, 500])
def test_a_provider_failure_over_http_shows_no_key(fake_provider, caplog, status):
    caplog.set_level(logging.DEBUG)
    fake_provider.reply = lambda body: (status, {"error": f"bad key {FAKE_KEY}"})

    response = _probe_app().post(
        "/probe", headers={"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY}
    )

    assert response.status_code == 500  # T2 maps provider errors to plain words
    assert FAKE_KEY not in response.text
    assert FAKE_KEY not in caplog.text


def test_a_read_over_http_uses_the_key(fake_provider, caplog):
    caplog.set_level(logging.DEBUG)

    response = _probe_app().post(
        "/probe", headers={"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY}
    )

    assert response.json() == {"text": "# Page"}
    assert fake_provider.requests[0][1]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert FAKE_KEY not in caplog.text


def test_a_demo_code_over_http_reaches_the_gateway_setting(demo_on, monkeypatch):
    seen = []

    def capture(vision: RequestVision) -> dict:
        seen.append(vision)
        return {}

    probe = FastAPI(exception_handlers=app.exception_handlers)
    probe.dependency_overrides = app.dependency_overrides
    probe.post("/probe")(capture)

    TestClient(probe).post("/probe", headers={"X-Demo-Code": DEMO_CODE})

    assert seen[0].base_url == GATEWAY
    assert seen[0].api_key == DEMO_CODE


def _pdf() -> bytes:
    with pymupdf.open() as doc:
        doc.new_page().insert_text((72, 72), "Hello from the text layer.")
        return doc.tobytes()


client = TestClient(app)
HEADERS = {"X-Model-Preset": "openai", "X-Model-Key": FAKE_KEY, "X-Demo-Code": FAKE_KEY}


def test_extract_ignores_the_credential_headers(fake_provider, caplog, demo_on):
    caplog.set_level(logging.DEBUG)
    files = {"file": ("a.pdf", _pdf(), "application/pdf")}

    response = client.post(
        "/v1/extract", files=files, data={"method": "vision", "format": "json"}, headers=HEADERS
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "vision_not_configured"
    assert fake_provider.requests == []
    assert FAKE_KEY not in response.text and FAKE_KEY not in caplog.text


@pytest.fixture
def store():
    saved = app.state.jobs
    app.state.jobs = JobStore(max_jobs=3, ttl=60)
    yield
    app.state.jobs.shutdown()
    app.state.jobs = saved


def test_jobs_ignore_the_credential_headers(fake_provider, caplog, demo_on, store):
    caplog.set_level(logging.DEBUG)
    files = {"file": ("a.pdf", _pdf(), "application/pdf")}

    refused = client.post("/v1/jobs", files=files, data={"method": "vision"}, headers=HEADERS)
    created = client.post(
        "/v1/jobs", files=files, data={"method": "auto", "format": "json"}, headers=HEADERS
    )
    assert created.status_code == 202
    job_id = created.json()["id"]
    for _ in range(200):
        job = client.get(f"/v1/jobs/{job_id}", headers=HEADERS)
        if job.json()["status"] in ("done", "failed"):
            break
        time.sleep(0.05)

    assert refused.json()["error"]["code"] == "vision_not_configured"
    assert job.json()["status"] == "done"
    assert job.json()["result"]["pages"][0]["method"] == "text_layer"
    assert fake_provider.requests == []
    for text in (refused.text, created.text, job.text, caplog.text):
        assert FAKE_KEY not in text


def test_capabilities_never_show_a_key_or_url_with_credentials_sent(demo_on):
    app.dependency_overrides[get_vision_config] = lambda: VisionConfig(
        base_url="https://llm.internal.example/v1", api_key="sk-operator", default_model="m"
    )

    response = client.get("/v1/capabilities", headers=HEADERS)

    assert response.status_code == 200
    assert FAKE_KEY not in response.text
    assert "sk-operator" not in response.text
    assert "http" not in response.text
    assert "gateway.internal" not in response.text
