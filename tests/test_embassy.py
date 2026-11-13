"""docker-compose.embassy.yml, the private review deploy: no host port, no model server."""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
COMPOSE_FILE = ROOT / "docker-compose.embassy.yml"
COMPOSE = yaml.safe_load(COMPOSE_FILE.read_text())
DOC = (ROOT / "docs" / "deploy" / "embassy.md").read_text()
ENV_NAMES = {
    "OCR_LLM_BASE_URL",
    "OCR_LLM_API_KEY",
    "OCR_LLM_DEFAULT_MODEL",
    "OCR_LLM_ALLOWED_MODELS",
}


def api() -> dict:
    return COMPOSE["services"]["api"]


def test_one_api_service_and_no_model_server():
    assert set(COMPOSE["services"]) == {"api"}
    assert "profiles" not in api()


def test_the_project_name_gives_the_image_the_receiver_tags():
    # fleet-deploy's receiver tags llm-pdf-ocr-api-api:latest as :rollback before a deploy.
    assert COMPOSE["name"] == "llm-pdf-ocr-api"
    assert "image" not in api()


def test_built_from_the_repo_dockerfile_with_no_source_mounted():
    assert api()["build"] in (".", {"context": "."})
    assert "volumes" not in api()
    assert "volumes" not in COMPOSE


def test_no_host_port():
    assert "ports" not in api()
    assert "network_mode" not in api()


def test_restarts_and_keeps_the_dockerfile_healthcheck():
    # No override here, so the image's own healthcheck (exercised by CI's docker job) holds.
    assert api()["restart"] == "unless-stopped"
    assert "healthcheck" not in api()


def test_on_the_external_proxy_network_as_ocr():
    assert COMPOSE["networks"] == {"proxy": {"external": True}}
    assert api()["networks"] == {"proxy": {"aliases": ["ocr"]}}


def test_settings_come_only_from_the_host_env_file():
    assert api()["env_file"] == [".env.embassy"]
    assert "environment" not in api()
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env.embassy"], cwd=ROOT, check=False
    ).returncode
    assert ignored == 0, ".env.embassy is not ignored by git"
    assert not (ROOT / ".env.embassy").exists()


def test_doc_names_every_variable_and_gives_no_value():
    for name in ENV_NAMES:
        assert f"`{name}`" in DOC, name
    assert "orchestrator.lan:4000" in DOC
    assert "ocr.estr.in" in DOC
    assert not re.search(r"sk-[A-Za-z0-9]", DOC)
    assert not re.search(r"OCR_LLM_API_KEY\s*=\s*\S", DOC)
