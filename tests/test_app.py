from fastapi.testclient import TestClient

import llm_pdf_ocr
from llm_pdf_ocr.app import app

client = TestClient(app)


def test_package_exposes_version():
    assert llm_pdf_ocr.__version__ == "0.0.1"


def test_health_reports_ok():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_models_route_is_gone():
    assert client.get("/models").status_code == 404
