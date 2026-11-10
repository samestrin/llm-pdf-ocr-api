"""The README "Try it" steps are the ones the CI `try-it` job runs, on a file that exists."""

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
README = (ROOT / "README.md").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
# CI's checkout stands in for these two; a PR branch is not on the public remote to clone.
CHECKOUT_STANDS_IN = ("git clone ", "cd ")


def try_it_section() -> str:
    match = re.search(r"^## Try it\n(.*?)(?=^## )", README, re.M | re.S)
    assert match, "README has no 'Try it' section"
    return match[1]


def try_it_commands() -> list[str]:
    blocks = re.findall(r"```bash\n(.*?)```", try_it_section(), re.S)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


def try_it_job() -> str:
    match = re.search(r"^  try-it:\n(.*?)(?=^  \S|\Z)", CI, re.M | re.S)
    assert match, "ci.yml has no try-it job"
    return match[1]


def test_try_it_has_the_steps():
    commands = try_it_commands()
    assert "docker compose up --build" in commands
    assert any(c.startswith("curl ") and "/v1/extract" in c for c in commands)


def test_every_command_is_in_the_ci_job():
    job = try_it_job()
    for command in try_it_commands():
        if command.startswith(CHECKOUT_STANDS_IN):
            continue
        assert command in job, command


def test_ci_job_waits_for_health_and_posts_the_sample():
    job = try_it_job()
    assert "/health" in job
    assert "/v1/extract" in job


def test_named_sample_exists():
    named = re.findall(r"tests/fixtures/[\w.]+\.pdf", try_it_section())
    assert "tests/fixtures/born_digital.pdf" in named
    for path in named:
        assert (ROOT / path).is_file(), path
