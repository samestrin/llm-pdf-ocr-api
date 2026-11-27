"""GET /LICENSE: the repo's MIT license, shipped in the wheel and served as plain text.

The footer's `MIT License` links here (same origin, no redirect), so the licence
travels with every deployment, including the built image and an API-only one.
"""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_pdf_ocr.app import _license_file, app

ROOT = Path(__file__).resolve().parents[1]
LICENSE_TEXT = (ROOT / "LICENSE").read_text(encoding="utf-8")

client = TestClient(app)


def test_license_is_served_as_plain_text_with_the_mit_text():
    response = client.get("/LICENSE")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == LICENSE_TEXT
    assert response.text.startswith("MIT License\n")
    assert "Copyright (c) 2024 Sam Estrin" in response.text


def test_license_is_served_directly_not_redirected():
    response = client.get("/LICENSE", follow_redirects=False)

    assert response.status_code == 200


def test_license_is_not_in_the_openapi_schema():
    assert "/LICENSE" not in client.get("/openapi.json").json()["paths"]


def test_license_is_served_even_when_the_web_page_is_off():
    # The MIT notice must travel with an API-only deployment too.
    app.state.web_page = False
    try:
        assert client.get("/LICENSE").status_code == 200
    finally:
        app.state.web_page = True


def test_the_license_shipped_inside_the_package_wins(tmp_path):
    package = tmp_path / "src" / "llm_pdf_ocr"
    package.mkdir(parents=True)
    (package / "LICENSE").write_text("shipped", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("checkout", encoding="utf-8")

    assert _license_file(package).read_text(encoding="utf-8") == "shipped"


def test_a_source_checkout_falls_back_to_the_repo_root_license(tmp_path):
    package = tmp_path / "src" / "llm_pdf_ocr"
    package.mkdir(parents=True)
    (tmp_path / "LICENSE").write_text("checkout", encoding="utf-8")

    assert _license_file(package).read_text(encoding="utf-8") == "checkout"


@pytest.fixture(scope="module")
def wheel(tmp_path_factory):
    out = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (built,) = out.glob("*.whl")
    return built


def test_the_wheel_ships_the_repo_license(wheel):
    with zipfile.ZipFile(wheel) as archive:
        shipped = archive.read("llm_pdf_ocr/LICENSE").decode("utf-8")

    assert shipped == LICENSE_TEXT


def test_license_is_served_from_the_wheel_not_the_source_tree(wheel, tmp_path):
    site = tmp_path / "site"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(site)
    code = (
        "import pathlib, sys\n"
        "import llm_pdf_ocr\n"
        "package = pathlib.Path(llm_pdf_ocr.__file__).parent\n"
        "assert package.is_relative_to(pathlib.Path(sys.argv[1])), package\n"
        "assert (package / 'LICENSE').is_file()\n"
        "from fastapi.testclient import TestClient\n"
        "from llm_pdf_ocr.app import app\n"
        "response = TestClient(app).get('/LICENSE')\n"
        "print(response.status_code)\n"
        "print(response.headers['content-type'])\n"
        "print(response.text, end='')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(site)],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, PYTHONPATH=str(site)),
    )

    assert result.returncode == 0, result.stderr
    status, content_type, text = result.stdout.split("\n", 2)
    assert status == "200"
    assert content_type.startswith("text/plain")
    assert text == LICENSE_TEXT
