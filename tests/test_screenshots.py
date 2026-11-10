import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docs" / "make_screenshots.py"
NAMES = [
    f"screenshot-web-page-{scheme}-{size}.png"
    for scheme in ("light", "dark")
    for size in ("desktop", "phone")
]


def test_every_readme_image_exists_and_has_alt_text():
    readme = (ROOT / "README.md").read_text()
    local = [
        (alt, src)
        for alt, src in re.findall(r"!\[([^\]]*)\]\(([^)\s]+)\)", readme)
        if not src.startswith("http")
    ]
    assert {Path(src).name for _, src in local} >= set(NAMES)
    for alt, src in local:
        assert (ROOT / src).is_file(), src
        assert alt.strip(), f"{src} has no alt text"


def test_old_screenshot_is_replaced():
    assert not (ROOT / "docs" / "screenshot-web-page.png").exists()


def test_script_header_command_writes_all_four_images(tmp_path):
    header = SCRIPT.read_text().split('"""')[1]
    assert "uv run python docs/make_screenshots.py [OUT_DIR]" in header

    subprocess.run([sys.executable, str(SCRIPT), str(tmp_path)], check=True, cwd=ROOT, timeout=180)

    for name in NAMES:
        assert (tmp_path / name).read_bytes().startswith(b"\x89PNG"), name


def test_changelog_links_to_files_that_exist():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    for src in re.findall(r"\]\((docs/[^)\s]+)\)", changelog):
        assert (ROOT / src).is_file(), src
