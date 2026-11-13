"""The README demo GIF: its frame-size guard, the checks on the encoded GIF, and the file.

The live capture itself (`uv run python docs/make_demo_gif.py`) needs Tesseract, chromium
and a real job, so it is not part of the test run. What is tested here is every check the
script makes, both ways, and the GIF it committed.
"""

import importlib.util
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "docs" / "make_demo_gif.py"
GIF = ROOT / "docs" / "demo-web-page.gif"
README = ROOT / "README.md"

_spec = importlib.util.spec_from_file_location("make_demo_gif", SCRIPT)
demo = importlib.util.module_from_spec(_spec)
sys.modules["make_demo_gif"] = demo
_spec.loader.exec_module(demo)

WRONG_SIZES = [(1281, 800), (1280, 801), (1279, 799), (640, 400), (800, 1280)]


def png(path: Path, size=demo.FRAME_SIZE, colour=(244, 245, 247)) -> Path:
    Image.new("RGB", size, colour).save(path)
    return path


def gif(path: Path, sizes, duration=demo.FRAME_MS) -> Path:
    """Write a GIF of the given frame sizes, the way the script writes its own."""
    frames = [Image.new("RGB", size, (200 + i, 30, 60)) for i, size in enumerate(sizes)]
    frames[0].save(
        path,
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=0,
        disposal=2,
        optimize=False,
    )
    return path


def test_one_frame_size_and_a_short_gif_are_the_rules():
    assert demo.FRAME_SIZE == (1280, 800)
    assert demo.MAX_SECONDS == 30
    assert demo.FRAME_MS * 2 < demo.MAX_SECONDS * 1000


def test_load_frames_accepts_a_set_of_frames_at_the_one_size(tmp_path):
    paths = [png(tmp_path / f"frame-{i}.png") for i in range(3)]

    frames = demo.load_frames(paths)

    assert [frame.size for frame in frames] == [demo.FRAME_SIZE] * 3


@pytest.mark.parametrize("size", WRONG_SIZES)
def test_load_frames_rejects_a_mixed_size_set(tmp_path, size):
    paths = [
        png(tmp_path / "frame-0.png"),
        png(tmp_path / "frame-1.png", size=size),
        png(tmp_path / "frame-2.png"),
    ]

    with pytest.raises(demo.DemoError) as error:
        demo.load_frames(paths)

    message = str(error.value)
    assert "frame-1.png" in message
    assert f"{size[0]}x{size[1]}" in message
    assert "1280x800" in message


def test_load_frames_rejects_a_set_with_no_frames(tmp_path):
    with pytest.raises(demo.DemoError, match="no frames"):
        demo.load_frames([])


@pytest.mark.parametrize("size", WRONG_SIZES[:2])
def test_stitch_refuses_a_frame_at_another_size(tmp_path, size):
    frames = [Image.new("RGB", demo.FRAME_SIZE), Image.new("RGB", size)]

    with pytest.raises(demo.DemoError, match="1280x800"):
        demo.stitch(frames, tmp_path / "demo.gif")


def test_stitch_writes_a_gif_that_the_checks_accept(tmp_path):
    # Different pictures: Pillow merges identical frames into one longer frame.
    paths = [png(tmp_path / f"frame-{i}.png", colour=(240 - i * 20, 245, 247)) for i in range(3)]
    frames = demo.load_frames(paths)
    out = tmp_path / "demo.gif"

    demo.stitch(frames, out)
    facts = demo.verify_gif(out)

    assert facts["frames"] == 3
    assert facts["size"] == demo.FRAME_SIZE
    assert facts["seconds"] == pytest.approx(3 * demo.FRAME_MS / 1000)


def test_verify_rejects_a_gif_that_does_not_animate(tmp_path):
    path = tmp_path / "still.gif"
    Image.new("RGB", demo.FRAME_SIZE, (10, 20, 30)).save(path, duration=demo.FRAME_MS, loop=0)

    with pytest.raises(demo.DemoError, match="animat"):
        demo.verify_gif(path)


def test_verify_rejects_a_gif_of_one_frame_size_that_is_not_the_one_size(tmp_path):
    path = gif(tmp_path / "small.gif", [(640, 400)] * 3)

    with pytest.raises(demo.DemoError, match="1280x800"):
        demo.verify_gif(path)


def test_verify_rejects_a_gif_whose_frames_change_size(tmp_path):
    # A smaller frame inside a 1280x800 canvas: Pillow reports the canvas size for it,
    # so the check has to look at the frame itself.
    path = gif(tmp_path / "mixed.gif", [demo.FRAME_SIZE, (640, 400), demo.FRAME_SIZE])

    with pytest.raises(demo.DemoError, match="1280x800"):
        demo.verify_gif(path)


@pytest.mark.parametrize("seconds", [30, 36])
def test_verify_rejects_a_gif_that_runs_too_long(tmp_path, seconds):
    count = seconds * 1000 // demo.FRAME_MS
    path = gif(tmp_path / "long.gif", [demo.FRAME_SIZE] * count)

    with pytest.raises(demo.DemoError, match="30"):
        demo.verify_gif(path)


def test_verify_rejects_a_file_that_is_not_a_gif(tmp_path):
    path = png(tmp_path / "frame.png")

    with pytest.raises(demo.DemoError):
        demo.verify_gif(path)


def test_verify_rejects_a_gif_whose_frames_say_no_duration(tmp_path):
    # Without a duration a viewer shows each frame as fast as it can: the run time of
    # such a GIF is not knowable, so it cannot be said to be under 30 seconds.
    path = tmp_path / "no-duration.gif"
    frames = [Image.new("RGB", demo.FRAME_SIZE, (200 + i, 30, 60)) for i in range(3)]
    frames[0].save(path, save_all=True, append_images=frames[1:], loop=0, disposal=2)

    with pytest.raises(demo.DemoError, match="duration"):
        demo.verify_gif(path)


def test_the_committed_gif_animates_at_one_frame_size_under_30_seconds():
    facts = demo.verify_gif(GIF)

    assert facts["frames"] >= 2
    assert facts["size"] == (1280, 800)
    assert facts["seconds"] < 30


def test_the_readme_shows_the_gif_with_alt_text_of_a_real_session():
    images = re.findall(r"!\[([^\]]*)\]\(([^)\s]+)\)", README.read_text())
    entries = [(alt, src) for alt, src in images if Path(src).name == GIF.name]

    assert len(entries) == 1, "the README shows the demo GIF once"
    alt, src = entries[0]
    assert (ROOT / src).is_file(), src
    # The alt text says what the recorded session does, not just "a demo".
    for word in ("Try a sample", "Tesseract", "find", "API reference"):
        assert word.lower() in alt.lower(), f"alt text does not mention {word}: {alt}"


def _fake_capture(monkeypatch, tmp_path):
    """Stand in for the browser and the server; record where main points the capture."""
    calls = {"bases": [], "servers": 0}

    def capture(base, out):
        calls["bases"].append(base)
        return [png(out / f"frame-{n}.png", colour=(n * 90, 0, 0)) for n in range(2)]

    def server():
        calls["servers"] += 1
        thread = threading.Thread(target=lambda: None)
        thread.start()
        return SimpleNamespace(should_exit=False), thread, "http://127.0.0.1:9"

    monkeypatch.setattr(demo, "capture_frames", capture)
    monkeypatch.setattr(demo, "start_server", server)
    monkeypatch.setattr(demo, "require_tesseract", lambda: None)
    return calls


def test_with_no_live_url_the_capture_runs_against_a_local_app(monkeypatch, tmp_path):
    monkeypatch.delenv("LIVE_URL", raising=False)
    calls = _fake_capture(monkeypatch, tmp_path)

    written = demo.main(tmp_path)

    assert calls == {"bases": ["http://127.0.0.1:9"], "servers": 1}
    assert written[-1] == tmp_path / demo.GIF_NAME


def test_live_url_points_the_capture_at_that_instance_and_starts_no_app(monkeypatch, tmp_path):
    monkeypatch.setenv("LIVE_URL", " https://ocr.example.test/ ")
    calls = _fake_capture(monkeypatch, tmp_path)

    demo.main(tmp_path)

    assert calls == {"bases": ["https://ocr.example.test"], "servers": 0}


def test_a_local_capture_needs_tesseract(monkeypatch):
    monkeypatch.setattr(demo.shutil, "which", lambda name: None)

    with pytest.raises(demo.DemoError, match="[Tt]esseract"):
        demo.require_tesseract()


def test_a_local_capture_accepts_an_installed_tesseract(monkeypatch):
    monkeypatch.setattr(demo.shutil, "which", lambda name: "/usr/bin/tesseract")

    demo.require_tesseract()
