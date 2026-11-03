"""The benchmark runner: scores, failed pages, the results file and the command."""

import importlib.util
import json
import random
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from llm_pdf_ocr.config import VisionConfig

BENCH = Path(__file__).parent.parent / "benchmarks"
_spec = importlib.util.spec_from_file_location("benchmark_run", BENCH / "run.py")
run = importlib.util.module_from_spec(_spec)
sys.modules["benchmark_run"] = run
_spec.loader.exec_module(run)

needs_tesseract = pytest.mark.skipif(
    shutil.which("tesseract") is None, reason="tesseract is not installed"
)

API_KEY = "sk-bench-secret-key-0123456789"
MODEL = "vision-bench"
BORN_DIGITAL = "us-constitution-text"
SCANNED = "us-constitution-scan"
TABLES = "mixtral-tables-p4"


def entries(*ids: str) -> list[dict]:
    chosen = [e for e in run.load_manifest(BENCH) if e["id"] in ids]
    assert len(chosen) == len(ids)
    return chosen


def naive_levenshtein(a: str, b: str) -> int:
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


# Character error rate


@pytest.mark.parametrize(
    "a, b",
    [("", ""), ("", "abc"), ("abc", ""), ("kitten", "sitting"), ("flaw", "lawn"), ("a", "a")],
)
def test_levenshtein_known_pairs(a, b):
    assert run.levenshtein(a, b) == naive_levenshtein(a, b)


def test_levenshtein_matches_the_textbook_algorithm_on_random_strings():
    rng = random.Random(5)
    for _ in range(300):
        a = "".join(rng.choices("abc d", k=rng.randint(0, 90)))
        b = "".join(rng.choices("abc d", k=rng.randint(0, 90)))
        assert run.levenshtein(a, b) == naive_levenshtein(a, b), (a, b)


def test_levenshtein_on_long_text_is_fast_enough():
    a = "The quick brown fox jumps over the lazy dog. " * 250
    b = a.replace("fox", "cat")
    assert run.levenshtein(a, b) == 3 * 250


def test_cer_is_edits_over_reference_length_after_normalising_whitespace():
    assert run.cer("Hello   world\n", "Hello world") == 0.0
    assert run.cer("Hellx world", "Hello world") == pytest.approx(1 / 11)


def test_cer_is_capped_at_one():
    assert run.cer("x" * 500, "abc") == 1.0
    assert run.cer("", "abc") == 1.0


def test_cer_ignores_markdown_tables_and_heading_marks():
    hypothesis = "# Title\n\nSome **body** text.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
    assert run.cer(hypothesis, "Title Some body text.") == 0.0


# Table recovery


REFERENCE_TABLE = "Table 1\n\n| Model | Score |\n|---|---|\n| A | 1.0 |\n| B | 2.0 |\n"


def test_table_cells_skip_the_separator_row_and_empty_cells():
    cells = run.table_cells("| Model | Score |\n|:---|---:|\n| A |  |\n")
    assert sorted(cells.elements()) == ["a", "model", "score"]


def test_table_f1_is_one_for_the_same_table():
    assert run.table_f1(REFERENCE_TABLE, REFERENCE_TABLE) == 1.0


def test_table_f1_counts_cells_not_layout():
    found = "| model | score |\n| --- | --- |\n| A | 1.0 |\n| B | 9.9 |\n"
    # 5 of 6 predicted cells are right (case is ignored), and 5 of 6 reference cells found.
    assert run.table_f1(found, REFERENCE_TABLE) == pytest.approx(5 / 6)


def test_table_f1_is_zero_when_the_output_has_no_table():
    assert run.table_f1("Model Score A 1.0 B 2.0", REFERENCE_TABLE) == 0.0


# Scoring documents


def test_text_layer_on_a_born_digital_file_scores_well():
    (result,) = run.run(entries(BORN_DIGITAL), ["text_layer"], VisionConfig(), BENCH)
    assert result.failed == 0
    assert result.score > 0.9
    assert result.cer == pytest.approx(1 - result.score)
    assert result.seconds_per_page >= 0


def test_a_forced_text_layer_run_on_a_scanned_file_is_failed_with_score_zero():
    (result,) = run.run(entries(SCANNED), ["text_layer"], VisionConfig(), BENCH)
    assert result.method == "text_layer"
    assert result.pages == 1
    assert result.failed == 1
    assert result.score == 0.0
    assert result.cer == 1.0


def test_a_failed_page_fails_the_table_score_too(monkeypatch):
    (result,) = run.run(entries(TABLES), ["text_layer"], VisionConfig(), BENCH)
    assert result.table_f1 is not None

    def broken(*args, **kwargs):
        raise RuntimeError("cannot read")

    monkeypatch.setattr(run, "extract_pdf", broken)
    (failed,) = run.run(entries(TABLES), ["text_layer"], VisionConfig(), BENCH)
    assert (failed.failed, failed.pages, failed.score, failed.table_f1) == (1, 1, 0.0, 0.0)


def test_a_failed_document_is_counted_in_the_summary_never_skipped():
    results = run.run(entries(BORN_DIGITAL, SCANNED), ["text_layer"], VisionConfig(), BENCH)
    (summary,) = run.summarize(results)
    good = next(r for r in results if r.entry_id == BORN_DIGITAL)
    assert (summary.documents, summary.pages, summary.failed) == (2, 2, 1)
    assert summary.score == pytest.approx(good.score / 2)
    assert summary.table_f1 is None


def test_entries_without_reference_tables_have_no_table_score():
    (result,) = run.run(entries(BORN_DIGITAL), ["text_layer"], VisionConfig(), BENCH)
    assert result.table_f1 is None


# Methods


def test_vision_runs_only_when_a_base_url_is_set():
    assert run.available_methods(VisionConfig(), tesseract=True) == ["text_layer", "tesseract"]
    configured = VisionConfig(base_url="http://x/v1", default_model="m", allowed_models=("m",))
    assert run.available_methods(configured, tesseract=True) == [
        "text_layer",
        "tesseract",
        "vision",
    ]


def test_tesseract_runs_only_when_it_is_installed(monkeypatch):
    assert run.available_methods(VisionConfig(), tesseract=False) == ["text_layer"]
    monkeypatch.setattr(run.shutil, "which", lambda name: None)
    assert run.available_methods(VisionConfig()) == ["text_layer"]


def test_a_date_that_is_not_a_date_is_an_error(tmp_path, capsys):
    code = run.main(["--only", BORN_DIGITAL, "--out", str(tmp_path), "--date", "../escape"])
    assert code == 2
    assert "--date" in capsys.readouterr().err
    assert not list(tmp_path.parent.glob("escape*"))


def test_asking_for_vision_without_a_backend_is_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OCR_LLM_BASE_URL", raising=False)
    code = run.main(["--methods", "vision", "--only", SCANNED, "--out", str(tmp_path)])
    assert code == 2
    assert "OCR_LLM_BASE_URL" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_an_unknown_entry_is_an_error(tmp_path, capsys):
    code = run.main(["--only", "no-such-file", "--out", str(tmp_path)])
    assert code == 2
    assert "no-such-file" in capsys.readouterr().err


# The command and its results file


@needs_tesseract
def test_one_command_writes_a_dated_table_for_text_layer_and_tesseract(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("OCR_LLM_BASE_URL", raising=False)
    code = run.main(
        ["--only", f"{BORN_DIGITAL},{SCANNED}", "--out", str(tmp_path), "--date", "2026-11-03"]
    )
    assert code == 0
    written = tmp_path / "2026-11-03.md"
    text = written.read_text(encoding="utf-8")
    assert text in capsys.readouterr().out
    header = next(line for line in text.splitlines() if line.startswith("| Method"))
    for column in ("Text score (1 − CER)", "CER", "Table F1", "Seconds per page", "Failed"):
        assert column in header
    rows = {line.split("|")[1].strip(): line for line in text.splitlines() if "| 2 |" in line}
    assert set(rows) == {"text_layer", "tesseract"}
    # Text layer fails on the scan; Tesseract reads both.
    assert rows["text_layer"].rstrip(" |").endswith("1")
    assert rows["tesseract"].rstrip(" |").endswith("0")
    assert "Vision model: not measured" in text
    assert json.loads((tmp_path / "2026-11-03.json").read_text())["date"] == "2026-11-03"


class FakeVision:
    """An OpenAI-compatible endpoint that answers every page with the same Markdown."""

    def __init__(self, reply: str, status: int = 200):
        payload = json.dumps(
            {"model": MODEL, "choices": [{"message": {"role": "assistant", "content": reply}}]}
        ).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_vision():
    reference = (BENCH / "reference" / f"{TABLES}.tables.md").read_text(encoding="utf-8")
    server = FakeVision(reference)
    yield server
    server.close()


def test_results_name_the_vision_model_but_never_the_url_or_key(
    tmp_path, monkeypatch, fake_vision, capsys
):
    monkeypatch.setenv("OCR_LLM_BASE_URL", fake_vision.base_url)
    monkeypatch.setenv("OCR_LLM_API_KEY", API_KEY)
    monkeypatch.setenv("OCR_LLM_DEFAULT_MODEL", MODEL)
    code = run.main(
        ["--methods", "vision", "--only", TABLES, "--out", str(tmp_path), "--date", "2026-11-03"]
    )
    assert code == 0
    files = sorted(tmp_path.iterdir())
    assert [f.name for f in files] == ["2026-11-03.json", "2026-11-03.md"]
    for written in [*files]:
        text = written.read_text(encoding="utf-8")
        assert MODEL in text
        assert API_KEY not in text
        assert fake_vision.base_url not in text
        assert "127.0.0.1" not in text
        assert str(fake_vision.httpd.server_port) not in text
    data = json.loads(files[0].read_text())
    (summary,) = data["summary"]
    assert (summary["method"], summary["model"], summary["table_f1"]) == ("vision", MODEL, 1.0)
    assert API_KEY not in capsys.readouterr().out


def test_a_vision_page_that_fails_is_counted_failed_even_with_the_tesseract_fallback():
    server = FakeVision("unused", status=500)
    try:
        vision = VisionConfig(
            base_url=server.base_url,
            api_key=API_KEY,
            default_model=MODEL,
            allowed_models=(MODEL,),
            timeout=5.0,
            fallback_to_tesseract=True,
        )
        (result,) = run.run(entries(BORN_DIGITAL), ["vision"], vision, BENCH)
    finally:
        server.close()
    assert (result.method, result.model, result.failed, result.score) == ("vision", MODEL, 1, 0.0)


def test_a_results_file_that_would_hold_the_url_or_key_is_refused(tmp_path):
    vision = VisionConfig(base_url="https://llm.example.com/v1", api_key=API_KEY)
    for leaked in ("see https://llm.example.com/v1", f"key {API_KEY}", "host llm.example.com"):
        with pytest.raises(run.SecretInResultsError):
            run.write_results({"note": leaked}, "x", tmp_path, "2026-11-03", vision)
    assert not list(tmp_path.iterdir())


def test_a_bare_host_name_that_is_also_the_model_name_is_not_a_leak(tmp_path):
    # docker-compose's Ollama service is reached as http://ollama:11434/v1.
    vision = VisionConfig(base_url="http://ollama:11434/v1", default_model="ollama-vision")
    paths = run.write_results({"model": "ollama-vision"}, "x", tmp_path, "2026-11-03", vision)
    assert [p.name for p in paths] == ["2026-11-03.json", "2026-11-03.md"]
    with pytest.raises(run.SecretInResultsError):
        run.write_results({"note": "ollama:11434"}, "x", tmp_path, "2026-11-04", vision)
