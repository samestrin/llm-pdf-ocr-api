"""Record the README's demo GIF from a real session of the app.

Run from the repository root (Tesseract installed, chromium installed with
`uv run playwright install chromium`):

    uv run python docs/make_demo_gif.py [OUT_DIR]

OUT_DIR defaults to docs/. Writes one 1280x800 PNG per step (demo-frame-NN-*.png, any
other size is refused), stitches them into demo-web-page.gif with Pillow, then reopens
that GIF and checks that it animates, that every frame is the one size, and that it runs
under 30 s.

Nothing is stubbed. The app runs on an ephemeral 127.0.0.1 port, "Try a sample" reads the
bundled mixed.pdf for real (its scanned pages need Tesseract), the find box steps through
real matches, and /docs is the real Scalar reference. An operator who runs their own
instance may point the capture at it with LIVE_URL; there is no default URL, and the
script never contacts one on its own.

The capture is not part of the test run: `uv run pytest` checks these guards and the
committed GIF (tests/test_demo_gif.py), never the recording.
"""

import os
import re
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
from PIL import Image
from playwright.sync_api import expect, sync_playwright

from llm_pdf_ocr.app import app

FRAME_SIZE = (1280, 800)
FRAME_MS = 2500  # eight frames at this duration run 20 s, under the 30 s limit
MAX_SECONDS = 30
GIF_NAME = "demo-web-page.gif"
FRAME_STEM = "demo-frame"
FIND_QUERY = "text"  # in the sample's scanned pages and in the Markdown output
FIND_STEPS = 3  # the match the search lands on, then two steps
JOB_TIMEOUT_MS = 180_000  # two pages through Tesseract on a CPU
RENDER_TIMEOUT_MS = 30_000


class DemoError(RuntimeError):
    """A frame, or the finished GIF, is not what the README's demo must be."""


def _require_size(image: Image.Image, label: str) -> None:
    if image.size != FRAME_SIZE:
        width, height = image.size
        raise DemoError(f"{label} is {width}x{height}, expected {FRAME_SIZE[0]}x{FRAME_SIZE[1]}")


def load_frames(paths: list[Path]) -> list[Image.Image]:
    """Open every captured frame, refusing a set with any size other than FRAME_SIZE."""
    if not paths:
        raise DemoError("no frames to stitch")
    frames = []
    for path in paths:
        with Image.open(path) as image:
            _require_size(image, f"frame {path.name}")
            frames.append(image.convert("RGB"))
    return frames


def stitch(frames: list[Image.Image], out: Path, duration_ms: int = FRAME_MS) -> Path:
    """Write the frames as one looping GIF, every frame a full FRAME_SIZE image."""
    if not frames:
        raise DemoError("no frames to stitch")
    for number, frame in enumerate(frames, start=1):
        _require_size(frame, f"frame {number}")
    first, *rest = frames
    # disposal=2 keeps Pillow from writing only the changed rectangle of a frame: each
    # frame is then the whole picture, which is what verify_gif checks for.
    first.save(
        out,
        save_all=True,
        append_images=rest,
        duration=duration_ms,
        loop=0,
        disposal=2,
        optimize=False,
    )
    return out


def verify_gif(path: Path) -> dict:
    """Reopen the written GIF: it must animate, at one frame size, under MAX_SECONDS."""
    with Image.open(path) as gif:
        count = getattr(gif, "n_frames", 1)
        if not getattr(gif, "is_animated", False) or count < 2:
            raise DemoError(f"{path.name} does not animate: {count} frame(s)")
        seconds = 0.0
        for number in range(count):
            gif.seek(number)
            label = f"{path.name} frame {number + 1}"
            _require_size(gif, label)
            # A frame smaller than the canvas still reports the canvas size, so the
            # frame's own extent is what proves every frame is the one size.
            extent = gif.tile[0][1] if gif.tile else (0, 0, *gif.size)
            if extent != (0, 0, *FRAME_SIZE):
                raise DemoError(
                    f"{label} covers {extent[2]}x{extent[3]}, "
                    f"expected {FRAME_SIZE[0]}x{FRAME_SIZE[1]}"
                )
            duration = gif.info.get("duration", 0)
            if duration <= 0:
                raise DemoError(f"{label} says no duration: every frame must say how long it shows")
            seconds += duration / 1000
    if seconds >= MAX_SECONDS:
        raise DemoError(f"{path.name} runs {seconds:.1f} s, must be under {MAX_SECONDS} s")
    return {"frames": count, "size": FRAME_SIZE, "seconds": seconds}


def require_tesseract() -> None:
    """The local app reads the sample's scanned pages with the `tesseract` command."""
    if shutil.which("tesseract") is None:
        raise DemoError("tesseract is not installed: the sample's scanned pages need it")


def start_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    return server, thread, f"http://127.0.0.1:{port}"


def _images_loaded(page) -> None:
    page.wait_for_function(
        "() => [...document.images].every((i) => i.complete && i.naturalWidth > 0)",
        timeout=RENDER_TIMEOUT_MS,
    )


def capture_frames(base: str, out: Path) -> list[Path]:
    """Drive one real session at FRAME_SIZE and save a PNG at each step."""
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport={"width": FRAME_SIZE[0], "height": FRAME_SIZE[1]}, color_scheme="light"
        )
        page = context.new_page()

        def shoot(step: str) -> None:
            path = out / f"{FRAME_STEM}-{len(written) + 1:02d}-{step}.png"
            page.screenshot(path=str(path))
            written.append(path)

        # The page before a file is chosen, then the sample read by the real app.
        page.goto(base + "/")
        expect(page.locator("#sample")).to_be_enabled(timeout=RENDER_TIMEOUT_MS)
        shoot("empty")

        page.click("#sample")
        expect(page.locator("#status")).to_have_text(re.compile(r"^Done"), timeout=JOB_TIMEOUT_MS)
        _images_loaded(page)
        first = page.locator("#pages li").first
        first.scroll_into_view_if_needed()
        expect(first.locator(".method")).to_contain_text("Text layer")
        shoot("result-text-layer")

        second = page.locator("#pages li").nth(1)
        # Centred, not just in view: the frame is about the scanned page and its confidence.
        second.evaluate("(card) => card.scrollIntoView({ block: 'center' })")
        expect(second.locator(".method")).to_contain_text("Tesseract")
        shoot("result-tesseract")

        # The find box: the first match, then two steps through the rest.
        page.locator("#find-input").fill(FIND_QUERY)
        expect(page.locator("#find-count")).to_have_text(
            re.compile(r"^1 of \d+$"), timeout=RENDER_TIMEOUT_MS
        )
        total = int(page.locator("#find-count").inner_text().split(" of ")[1])
        if total < FIND_STEPS:
            raise DemoError(f"the sample has {total} matches for {FIND_QUERY!r}, need {FIND_STEPS}")
        expect(page.locator("mark.find-match.current")).to_be_in_viewport()
        shoot("find-first")
        for step in range(2, FIND_STEPS + 1):
            page.click("#find-next")
            expect(page.locator("#find-count")).to_have_text(
                re.compile(rf"^{step} of \d+$"), timeout=RENDER_TIMEOUT_MS
            )
            expect(page.locator("mark.find-match.current")).to_be_in_viewport()
            shoot(f"find-step-{step}")

        # The API reference the footer links to.
        page.goto(base + "/docs")
        expect(page.get_by_role("link", name="Capabilities")).to_be_visible(
            timeout=RENDER_TIMEOUT_MS
        )
        shoot("api-reference")
        page.locator('a[href="#POST/v1/extract"]').first.click()
        extract = page.get_by_role("heading", name="Extract", exact=True)
        expect(extract).to_be_visible(timeout=RENDER_TIMEOUT_MS)
        extract.scroll_into_view_if_needed()
        shoot("api-extract")

        context.close()
        browser.close()
    return written


def main(out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    live = os.environ.get("LIVE_URL", "").strip()
    if live:
        frames = capture_frames(live.rstrip("/"), out)
    else:
        require_tesseract()
        server, thread, base = start_server()
        try:
            frames = capture_frames(base, out)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
    gif = stitch(load_frames(frames), out / GIF_NAME)
    verify_gif(gif)
    return [*frames, gif]


if __name__ == "__main__":
    for path in main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent):
        print(path)
