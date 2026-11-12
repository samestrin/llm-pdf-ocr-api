"""The release workflow: tag guard, triggers, permissions and pinned actions."""

import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
GUARD = ROOT / ".github" / "scripts" / "check-tag-version.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def run_guard(tag: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(GUARD), tag], cwd=ROOT, capture_output=True, text=True, check=False)


def load_workflow() -> dict:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    # YAML 1.1 reads the bare key `on` as True.
    workflow["on"] = workflow.pop(True)
    return workflow


def test_guard_accepts_the_pyproject_version():
    result = run_guard(f"v{VERSION}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("tag", ["v0.0.1", f"v{VERSION}.1", VERSION, f"v{VERSION}-rc1"])
def test_guard_fails_on_a_mismatched_tag(tag):
    result = run_guard(tag)
    assert result.returncode != 0
    assert f"does not match pyproject.toml version {VERSION}" in result.stderr


def test_triggers_on_v_tags_and_on_pull_requests_touching_the_workflow():
    on = load_workflow()["on"]
    assert on["push"] == {"tags": ["v*"]}
    assert ".github/workflows/release.yml" in on["pull_request"]["paths"]


def test_packages_write_is_granted_to_the_publish_job_only():
    workflow = load_workflow()
    assert workflow["permissions"] == {"contents": "read"}
    granted = {
        name: job.get("permissions", {}).get("packages") for name, job in workflow["jobs"].items()
    }
    assert granted == {"dry-run": None, "publish": "write"}


def test_dry_run_never_pushes_and_publish_pushes_version_and_latest():
    jobs = load_workflow()["jobs"]
    assert jobs["dry-run"]["if"] == "github.event_name == 'pull_request'"
    assert "github.event_name == 'push'" in jobs["publish"]["if"]

    def build_step(job):
        (step,) = [s for s in job["steps"] if "build-push-action" in s.get("uses", "")]
        return step["with"]

    assert build_step(jobs["dry-run"])["push"] is False
    publish = build_step(jobs["publish"])
    assert publish["push"] is True
    assert publish["tags"].splitlines() == [
        "${{ env.IMAGE }}:${{ env.VERSION }}",
        "${{ env.IMAGE }}:latest",
    ]
    assert load_workflow()["env"]["IMAGE"] == "ghcr.io/samestrin/llm-pdf-ocr-api"


def test_every_action_is_pinned_to_a_commit_sha():
    uses = [
        step["uses"]
        for job in load_workflow()["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def changelog_sections() -> dict[str, str]:
    text = (ROOT / "CHANGELOG.md").read_text()
    parts = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    return {part.splitlines()[0]: part.split("\n", 1)[1] for part in parts}


def test_changelog_has_a_1_0_0_section_with_the_release_workflow_entry():
    # The 1.0.0 release is history: its section keeps its date and entries untouched.
    sections = changelog_sections()
    heading = "1.0.0 (2026-11-10)"
    assert heading in sections
    assert "Release workflow (`.github/workflows/release.yml`)" in sections[heading]


def test_changelog_unreleased_is_empty_and_top_version_is_pyproject_version():
    sections = changelog_sections()
    assert sections["Unreleased"].strip() == ""
    top = [h for h in sections if h != "Unreleased"][0]
    assert re.fullmatch(rf"{re.escape(VERSION)} \(\d{{4}}-\d{{2}}-\d{{2}}\)", top)
    assert "find box above the result" in sections[top]
    assert "API reference at `/docs` now uses Scalar" in sections[top]
    assert "README demo GIF" in sections[top]
    assert "CI checks the API reference end to end" in sections[top]
