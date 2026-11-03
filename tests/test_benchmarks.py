"""Every benchmark PDF has a source URL, a license and reference text in the manifest."""

import json
from pathlib import Path
from urllib.parse import urlsplit

import pymupdf
import pytest

BENCH = Path(__file__).parent.parent / "benchmarks"
ENTRIES = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))["entries"]
KINDS = {"born-digital", "two-column", "tables", "form", "scanned", "poor-scan"}


def ids() -> list[str]:
    return [e["id"] for e in ENTRIES]


def test_every_pdf_under_benchmarks_has_a_manifest_entry():
    on_disk = {p.relative_to(BENCH).as_posix() for p in BENCH.rglob("*.pdf")}
    listed = {e["pdf"] for e in ENTRIES}
    assert on_disk, "no benchmark PDFs found"
    assert on_disk == listed


def test_manifest_ids_are_unique():
    assert len(set(ids())) == len(ids())


def test_about_twenty_files_covering_every_kind():
    assert 18 <= len(ENTRIES) <= 24
    assert {e["kind"] for e in ENTRIES} == KINDS


@pytest.mark.parametrize("entry", ENTRIES, ids=ids())
def test_entry_has_source_license_and_reference_text(entry):
    source = urlsplit(entry["source_url"])
    assert source.scheme == "https" and source.netloc
    assert entry["license"].strip()
    assert urlsplit(entry["license_url"]).scheme == "https"
    assert entry["attribution"].strip()
    reference = BENCH / entry["reference_text"]
    assert reference.read_text(encoding="utf-8").strip()
    assert (BENCH / entry["pdf"]).read_bytes().startswith(b"%PDF")


@pytest.mark.parametrize("entry", ENTRIES, ids=ids())
def test_reference_files_all_belong_to_an_entry(entry):
    stem = Path(entry["pdf"]).stem
    assert Path(entry["reference_text"]).stem == stem


def test_no_orphan_reference_files():
    listed = {e["reference_text"] for e in ENTRIES} | {
        e["reference_tables"] for e in ENTRIES if "reference_tables" in e
    }
    on_disk = {p.relative_to(BENCH).as_posix() for p in (BENCH / "reference").iterdir()}
    assert on_disk == listed


@pytest.mark.parametrize(
    "entry", [e for e in ENTRIES if e["kind"] == "tables"], ids=lambda e: e["id"]
)
def test_table_files_have_markdown_reference_tables(entry):
    lines = (BENCH / entry["reference_tables"]).read_text(encoding="utf-8").splitlines()
    separators = [i for i, line in enumerate(lines) if set(line) <= set("|-: ") and "---" in line]
    assert separators, "no Markdown table separator row"
    for i in separators:
        width = lines[i].count("|")
        assert lines[i - 1].count("|") == width
        body = []
        for line in lines[i + 1 :]:
            if not line.startswith("|"):
                break
            body.append(line)
        assert body and all(line.count("|") == width for line in body)


@pytest.mark.parametrize(
    "entry", [e for e in ENTRIES if e["kind"] in ("scanned", "poor-scan")], ids=lambda e: e["id"]
)
def test_scans_have_no_text_layer(entry):
    with pymupdf.open(BENCH / entry["pdf"]) as doc:
        assert all(not page.get_text().strip() for page in doc)


@pytest.mark.parametrize(
    "entry",
    [e for e in ENTRIES if e["kind"] not in ("scanned", "poor-scan")],
    ids=lambda e: e["id"],
)
def test_digital_files_have_a_text_layer(entry):
    with pymupdf.open(BENCH / entry["pdf"]) as doc:
        assert all(page.get_text().strip() for page in doc)
