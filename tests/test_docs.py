"""The env examples in docs/vision-backend.md work against a fake OpenAI-compatible server."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from test_vision import completion, server  # noqa: F401  (server is a fixture)

from llm_pdf_ocr.app import app, get_vision_config
from llm_pdf_ocr.config import load_vision_config

DOC = Path(__file__).parent.parent / "docs" / "vision-backend.md"
MIXED = (Path(__file__).parent / "fixtures" / "mixed.pdf").read_bytes()
SCAN = "2"  # mixed.pdf: page 2 is only an image
KNOWN = {
    "OCR_LLM_BASE_URL",
    "OCR_LLM_API_KEY",
    "OCR_LLM_DEFAULT_MODEL",
    "OCR_LLM_ALLOWED_MODELS",
    "OCR_LLM_TIMEOUT_SECONDS",
    "OCR_LLM_CONCURRENCY",
    "OCR_LLM_FALLBACK_TO_TESSERACT",
}


def env_blocks() -> dict[str, dict[str, str]]:
    """Each ```dotenv block in the page, keyed by the heading above it."""
    blocks, heading = {}, None
    for match in re.finditer(r"^#+ (.+)$|^```dotenv\n(.*?)^```$", DOC.read_text(), re.M | re.S):
        if match[1]:
            heading = match[1].strip()
            continue
        env = {}
        for line in match[2].splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                name, _, value = line.partition("=")
                env[name.strip()] = value.strip()
        blocks[heading] = env
    return blocks


BLOCKS = env_blocks()


def test_the_page_has_a_vllm_an_ollama_and_a_cloud_example():
    names = " ".join(BLOCKS).lower()
    assert "vllm" in names
    assert "ollama" in names
    assert "cloud" in names


@pytest.mark.parametrize("heading", BLOCKS)
def test_every_example_sets_only_known_variables(heading):
    env = BLOCKS[heading]
    assert set(env) <= KNOWN
    assert {"OCR_LLM_BASE_URL", "OCR_LLM_DEFAULT_MODEL"} <= set(env)


@pytest.mark.parametrize("heading", BLOCKS)
def test_every_base_url_is_the_v1_root_the_service_appends_chat_completions_to(heading):
    url = urlsplit(BLOCKS[heading]["OCR_LLM_BASE_URL"])
    assert url.scheme in {"http", "https"}
    assert url.path.rstrip("/").endswith("/v1")


def test_the_cloud_example_names_no_vendor():
    (cloud,) = [env for heading, env in BLOCKS.items() if "cloud" in heading.lower()]
    host = urlsplit(cloud["OCR_LLM_BASE_URL"]).hostname
    assert host == "example.com" or host.endswith(".example.com")
    assert cloud["OCR_LLM_BASE_URL"].startswith("https://")


@pytest.mark.parametrize("heading", BLOCKS)
def test_every_example_reads_a_scanned_page_with_its_default_model(server, heading):  # noqa: F811
    env = dict(BLOCKS[heading], OCR_LLM_BASE_URL=server.base_url)
    config = load_vision_config(env)
    server.reply = lambda body: (200, completion("# Scanned", model=body["model"]))
    app.dependency_overrides[get_vision_config] = lambda: config

    response = TestClient(app).post(
        "/v1/extract",
        files={"file": ("mixed.pdf", MIXED, "application/pdf")},
        data={"format": "json", "pages": SCAN},
    )

    assert response.status_code == 200, response.text
    (page,) = response.json()["pages"]
    assert (page["status"], page["method"]) == ("ok", "vision")
    assert page["model"] == env["OCR_LLM_DEFAULT_MODEL"]
    path, headers, body = server.requests[0]
    assert path == "/v1/chat/completions"
    assert body["model"] == env["OCR_LLM_DEFAULT_MODEL"]
    if env.get("OCR_LLM_API_KEY"):
        assert headers["Authorization"] == f"Bearer {env['OCR_LLM_API_KEY']}"
    else:
        assert "Authorization" not in headers
    assert server.base_url not in json.dumps(response.json())


@pytest.mark.parametrize("heading", BLOCKS)
def test_every_example_lets_a_caller_pick_only_the_listed_models(server, heading):  # noqa: F811
    env = dict(BLOCKS[heading], OCR_LLM_BASE_URL=server.base_url)
    config = load_vision_config(env)
    app.dependency_overrides[get_vision_config] = lambda: config

    response = TestClient(app).post(
        "/v1/extract",
        files={"file": ("mixed.pdf", MIXED, "application/pdf")},
        data={"pages": SCAN, "model": "not-on-the-list"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_not_allowed"
    assert server.requests == []
