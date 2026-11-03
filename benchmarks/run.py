"""Run the benchmark set through each method and write a dated results table.

    uv run python benchmarks/run.py [--only ID,ID] [--methods text_layer,tesseract,vision]

Every entry in `manifest.json` is read with each method: the text layer, Tesseract,
and the vision model only when `OCR_LLM_BASE_URL` is set (with the same `OCR_LLM_*`
settings the service reads). For each document it measures:

- CER, the character error rate: edit distance to the reference text over the
  reference's length, after collapsing whitespace, capped at 1. Markdown tables and
  heading marks in the output are left out, as the reference text leaves tables out.
  The text score is 1 - CER.
- Table F1, for entries with reference tables: the cells of the Markdown tables in the
  output against the reference table cells, compared as a multiset of cell texts.
  Output with no Markdown table recovers no cells.
- Seconds per page: the wall time to read the document over its page count.

A document with a failed page scores 0 (CER 1, table F1 0) and its failed pages are
counted in the `Failed` column; it stays in every average. The results go to
`benchmarks/results/<date>.md` and `<date>.json`, which name the vision model but
never the endpoint URL or key.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import date as Date
from pathlib import Path
from urllib.parse import urlsplit

from llm_pdf_ocr.config import ConfigError, VisionConfig, load_limits, load_vision_config
from llm_pdf_ocr.extract import MethodChoice, extract_pdf

BENCH = Path(__file__).parent
METHODS = ["text_layer", "tesseract", "vision"]


class SecretInResultsError(RuntimeError):
    """The results would contain the vision endpoint's URL, host or key."""


@dataclass(frozen=True)
class DocResult:
    entry_id: str
    kind: str
    method: str
    model: str | None
    pages: int
    failed: int
    cer: float
    table_f1: float | None
    seconds_per_page: float

    @property
    def score(self) -> float:
        return 1.0 - self.cer


@dataclass(frozen=True)
class Summary:
    method: str
    model: str | None
    documents: int
    pages: int
    failed: int
    cer: float
    table_f1: float | None
    seconds_per_page: float

    @property
    def score(self) -> float:
        return 1.0 - self.cer


# Character error rate


def levenshtein(a: str, b: str) -> int:
    """Edit distance between `a` and `b` (Myers' bit-parallel algorithm, Hyyrö's form)."""
    if not a:
        return len(b)
    mask = (1 << len(a)) - 1
    high = 1 << (len(a) - 1)
    peq: dict[str, int] = {}
    for i, c in enumerate(a):
        peq[c] = peq.get(c, 0) | (1 << i)
    pv, mv, score = mask, 0, len(a)
    for c in b:
        eq = peq.get(c, 0)
        xv = eq | mv
        xh = ((((eq & pv) + pv) & mask) ^ pv) | eq
        ph = (mv | ~(xh | pv)) & mask
        mh = pv & xh
        if ph & high:
            score += 1
        elif mh & high:
            score -= 1
        ph = ((ph << 1) | 1) & mask
        mh = (mh << 1) & mask
        pv = (mh | ~(xv | ph)) & mask
        mv = ph & xv
    return score


_TABLE_ROW = re.compile(r"^\s*\|.*$", re.MULTILINE)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"\*\*|__")


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _prose(text: str) -> str:
    """Output text without Markdown tables, heading marks or bold marks."""
    return _EMPHASIS.sub("", _HEADING.sub("", _TABLE_ROW.sub("", text)))


def cer(hypothesis: str, reference: str) -> float:
    """Character error rate of `hypothesis` against `reference`, from 0 to 1."""
    ref = _normalise(reference)
    hyp = _normalise(_prose(hypothesis))
    if not ref:
        return 0.0 if not hyp else 1.0
    return min(1.0, levenshtein(ref, hyp) / len(ref))


# Table recovery

_SEPARATOR_CELL = re.compile(r":?-+:?")


def table_cells(markdown: str) -> Counter:
    """The non-empty cells of every Markdown table in `markdown`, normalised."""
    cells: Counter = Counter()
    for row in _TABLE_ROW.findall(markdown):
        parts = [p.strip() for p in row.strip().strip("|").split("|")]
        if all(_SEPARATOR_CELL.fullmatch(p) for p in parts if p) and any(parts):
            continue
        for part in parts:
            cell = _normalise(_EMPHASIS.sub("", part)).lower()
            if cell:
                cells[cell] += 1
    return cells


def table_f1(hypothesis: str, reference: str) -> float:
    """Cell-level F1 of the tables in `hypothesis` against those in `reference`."""
    found, expected = table_cells(hypothesis), table_cells(reference)
    hits = sum((found & expected).values())
    if not hits:
        return 0.0
    precision = hits / sum(found.values())
    recall = hits / sum(expected.values())
    return 2 * precision * recall / (precision + recall)


# Running


def load_manifest(bench: Path = BENCH) -> list[dict]:
    return json.loads((bench / "manifest.json").read_text(encoding="utf-8"))["entries"]


def available_methods(vision: VisionConfig, tesseract: bool | None = None) -> list[str]:
    """The text layer always, Tesseract when installed, the vision model when configured."""
    if tesseract is None:
        tesseract = shutil.which("tesseract") is not None
    return [
        m
        for m in METHODS
        if m == "text_layer"
        or (m == "tesseract" and tesseract)
        or (m == "vision" and vision.enabled)
    ]


def score_entry(entry: dict, method: str, vision: VisionConfig, bench: Path) -> DocResult:
    """Read one benchmark PDF with `method` and score it against its references.

    The vision model is measured on its own: a page it fails is failed here, even when
    the operator lets Tesseract read such pages in the service.
    """
    vision = replace(vision, fallback_to_tesseract=False)
    data = (bench / entry["pdf"]).read_bytes()
    reference = (bench / entry["reference_text"]).read_text(encoding="utf-8")
    tables = entry.get("reference_tables")
    reference_tables = (bench / tables).read_text(encoding="utf-8") if tables else None
    model = vision.default_model if method == "vision" else None
    start = time.perf_counter()
    try:
        doc = extract_pdf(
            data,
            method=MethodChoice(method),
            vision=vision,
            page_timeout=load_limits().page_timeout,
        )
        pages, failed = doc.page_count, len(doc.failed_pages)
        text = "\n\n".join(p.text for p in doc.pages)
    except Exception:  # an unreadable document fails every page; it is never skipped
        pages, failed, text = len(entry["source_pages"]), len(entry["source_pages"]), ""
    elapsed = time.perf_counter() - start
    if failed:
        error, f1 = 1.0, (0.0 if reference_tables is not None else None)
    else:
        error = cer(text, reference)
        f1 = table_f1(text, reference_tables) if reference_tables is not None else None
    return DocResult(
        entry_id=entry["id"],
        kind=entry["kind"],
        method=method,
        model=model,
        pages=pages,
        failed=failed,
        cer=error,
        table_f1=f1,
        seconds_per_page=elapsed / max(pages, 1),
    )


def run(
    entries: list[dict], methods: list[str], vision: VisionConfig, bench: Path = BENCH
) -> list[DocResult]:
    return [score_entry(e, m, vision, bench) for m in methods for e in entries]


def summarize(results: list[DocResult]) -> list[Summary]:
    """One row per method; every document counts, failed ones as 0."""
    summaries = []
    for method in dict.fromkeys(r.method for r in results):
        rows = [r for r in results if r.method == method]
        tables = [r.table_f1 for r in rows if r.table_f1 is not None]
        pages = sum(r.pages for r in rows)
        summaries.append(
            Summary(
                method=method,
                model=rows[0].model,
                documents=len(rows),
                pages=pages,
                failed=sum(r.failed for r in rows),
                cer=sum(r.cer for r in rows) / len(rows),
                table_f1=sum(tables) / len(tables) if tables else None,
                seconds_per_page=sum(r.seconds_per_page * r.pages for r in rows) / max(pages, 1),
            )
        )
    return summaries


# Results


def _num(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


def tesseract_version() -> str | None:
    """The first line of `tesseract --version`, or None when it is not installed."""
    try:
        done = subprocess.run(["tesseract", "--version"], capture_output=True, text=True)
    except OSError:
        return None
    lines = (done.stdout or done.stderr).splitlines()
    return lines[0].strip() if lines else None


def render(
    results: list[DocResult],
    summaries: list[Summary],
    day: str,
    model: str | None,
    tesseract: str | None = None,
) -> str:
    vision = f"Vision model: `{model}`." if model else "Vision model: not measured."
    if tesseract:
        vision += f" Tesseract: {tesseract}."
    lines = [
        f"# Benchmark results, {day}",
        "",
        f"{vision} Produced by `uv run python benchmarks/run.py`; see `benchmarks/README.md`"
        " for how each column is measured. A document with a failed page scores 0 and its"
        " failed pages are counted under Failed.",
        "",
        "| Method | Model | Documents | Pages | Text score (1 − CER) | CER | Table F1"
        " | Seconds per page | Failed |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in summaries:
        lines.append(
            f"| {s.method} | {s.model or '—'} | {s.documents} | {s.pages} | {_num(s.score)}"
            f" | {_num(s.cer)} | {_num(s.table_f1)} | {s.seconds_per_page:.2f} | {s.failed} |"
        )
    lines += [
        "",
        "## Per document",
        "",
        "| Document | Kind | Method | Pages | Text score | CER | Table F1 | Seconds per page"
        " | Failed |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in results:
        lines.append(
            f"| {r.entry_id} | {r.kind} | {r.method} | {r.pages} | {_num(r.score)}"
            f" | {_num(r.cer)} | {_num(r.table_f1)} | {r.seconds_per_page:.2f} | {r.failed} |"
        )
    return "\n".join(lines) + "\n"


def _secrets(vision: VisionConfig) -> list[str]:
    secrets = [vision.api_key or ""]
    if vision.base_url:
        parts = urlsplit(vision.base_url)
        secrets += [vision.base_url, parts.netloc]
        # A dotted name or an address; a bare name such as `ollama` may be the model's too.
        if parts.hostname and ("." in parts.hostname or ":" in parts.hostname):
            secrets.append(parts.hostname)
    return [s for s in secrets if s]


def write_results(
    data: dict, markdown: str, out: Path, day: str, vision: VisionConfig
) -> list[Path]:
    """Write `<day>.json` and `<day>.md` to `out`, refusing if either names the endpoint."""
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    for secret in _secrets(vision):
        if secret in payload or secret in markdown:
            raise SecretInResultsError("the results would contain the vision endpoint or key")
    out.mkdir(parents=True, exist_ok=True)
    paths = [out / f"{day}.json", out / f"{day}.md"]
    paths[0].write_text(payload, encoding="utf-8")
    paths[1].write_text(markdown, encoding="utf-8")
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="comma-separated entry ids (default: every entry)")
    parser.add_argument("--methods", help="comma-separated methods (default: all available)")
    parser.add_argument("--out", type=Path, default=BENCH / "results")
    parser.add_argument("--date", default=Date.today().isoformat())
    args = parser.parse_args(argv)

    def fail(message: str) -> int:
        print(f"error: {message}", file=sys.stderr)
        return 2

    try:
        day = Date.fromisoformat(args.date).isoformat()
    except ValueError:
        return fail(f"--date must be YYYY-MM-DD, got {args.date!r}")

    try:
        vision = load_vision_config()
    except ConfigError as exc:
        return fail(str(exc))
    available = available_methods(vision)
    methods = args.methods.split(",") if args.methods else available
    for method in methods:
        if method == "vision" and not vision.enabled:
            return fail("the vision method needs OCR_LLM_BASE_URL (and OCR_LLM_DEFAULT_MODEL)")
        if method not in available:
            return fail(f"method {method!r} is not available; choose from {', '.join(available)}")
    entries = load_manifest()
    if args.only:
        wanted = args.only.split(",")
        missing = sorted(set(wanted) - {e["id"] for e in entries})
        if missing:
            return fail(f"no benchmark entry {', '.join(missing)}")
        entries = [e for e in entries if e["id"] in wanted]

    results = run(entries, methods, vision)
    summaries = summarize(results)
    model = vision.default_model if "vision" in methods else None
    tesseract = tesseract_version() if "tesseract" in methods else None
    data = {
        "date": day,
        "vision_model": model,
        "tesseract": tesseract,
        "summary": [asdict(s) | {"score": s.score} for s in summaries],
        "documents": [asdict(r) | {"score": r.score} for r in results],
    }
    markdown = render(results, summaries, day, model, tesseract)
    try:
        paths = write_results(data, markdown, args.out, day, vision)
    except SecretInResultsError as exc:
        return fail(str(exc))
    print(markdown)
    print(f"Wrote {', '.join(str(p) for p in paths)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
