"""The README vision row matches the newest benchmark result, before and after the owner's run."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()
RESULTS = sorted((ROOT / "benchmarks" / "results").glob("*.json"))


def newest() -> tuple[Path, dict]:
    return RESULTS[-1], json.loads(RESULTS[-1].read_text())


def readme_vision_cells() -> list[str]:
    rows = re.findall(r"^\| `vision` \|(.*)\|\s*$", README, re.M)
    assert len(rows) == 1, "the README benchmark table needs exactly one vision row"
    return [c.strip() for c in rows[0].split("|")]


def result_vision_row(data: dict) -> dict | None:
    rows = [r for r in data["summary"] if r["method"] == "vision"]
    return rows[0] if rows else None


def test_the_readme_vision_row_matches_the_newest_result():
    path, data = newest()
    cells = readme_vision_cells()
    row = result_vision_row(data)
    if cells == ["not measured"] * 4:
        assert row is None, f"{path.name} has a vision row but the README says not measured"
        return
    assert row is not None, f"README has vision numbers but {path.name} has no vision row"
    assert data["vision_model"] and row["model"] == data["vision_model"]
    expected = [
        f"{row['score']:.3f}",
        f"{row['table_f1']:.3f}",
        f"{row['seconds_per_page']:.2f}",
        str(row["failed"]),
    ]
    assert cells == expected


def test_the_result_files_hold_no_url_and_no_key():
    for path in RESULTS:
        text = path.read_text()
        assert "http" not in text, f"{path.name} holds a URL"
        assert not re.search(r"sk-[A-Za-z0-9]{8,}|api[_-]?key|bearer", text, re.I), (
            f"{path.name} holds a key"
        )
    for path in (ROOT / "benchmarks" / "results").glob("*.md"):
        assert "http" not in path.read_text(), f"{path.name} holds a URL"


def test_the_readme_gives_the_owner_the_exact_vision_steps():
    for step in (
        "docker compose --profile vision up -d",
        "docker compose exec ollama ollama pull qwen2.5vl:7b",
        "OCR_LLM_BASE_URL=",
        "OCR_LLM_DEFAULT_MODEL=qwen2.5vl:7b",
        "OCR_LLM_TIMEOUT_SECONDS=",
        "uv run python benchmarks/run.py",
    ):
        assert step in README
