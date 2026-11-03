import tomllib
from importlib.metadata import version
from pathlib import Path

from fastapi.testclient import TestClient

import llm_pdf_ocr
from llm_pdf_ocr.app import app

client = TestClient(app)


ROOT = Path(__file__).resolve().parents[1]


def test_package_version_comes_from_pyproject():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert llm_pdf_ocr.__version__ == pyproject["project"]["version"] == "1.0.0"
    assert llm_pdf_ocr.__version__ == version("llm-pdf-ocr-api")


def test_version_file_is_gone():
    assert not (ROOT / ".version").exists()


def test_openapi_reports_package_version():
    assert client.get("/openapi.json").json()["info"]["version"] == llm_pdf_ocr.__version__


def test_changelog_has_release_section():
    changelog = (ROOT / "CHANGELOG.md").read_text()

    assert f"\n## {llm_pdf_ocr.__version__}" in changelog


def test_health_reports_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_models_route_is_gone():
    assert client.get("/models").status_code == 404
