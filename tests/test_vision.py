import base64
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pymupdf
import pytest

from llm_pdf_ocr import vision
from llm_pdf_ocr.config import ModelNotAllowedError, VisionConfig
from llm_pdf_ocr.vision import VisionError, VisionResult, read_image, read_page, read_pdf_pages

API_KEY = "sk-test-secret-key-0123456789"
MODEL = "vision-small"


class FakeServer:
    """An OpenAI-compatible /chat/completions endpoint on a free local port."""

    def __init__(self):
        self.requests = []
        self.reply = lambda body: (200, completion("# Page"))
        self.delay = 0.0
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with fake._lock:
                    fake.requests.append((self.path, dict(self.headers), body))
                    fake.active += 1
                    fake.peak = max(fake.peak, fake.active)
                try:
                    time.sleep(fake.delay)
                    status, payload = fake.reply(body)
                finally:
                    with fake._lock:
                        fake.active -= 1
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def config(self, **overrides) -> VisionConfig:
        settings = dict(
            base_url=self.base_url,
            api_key=API_KEY,
            default_model=MODEL,
            allowed_models=(MODEL, "vision-large"),
            timeout=5.0,
            concurrency=2,
        )
        settings.update(overrides)
        return VisionConfig(**settings)


def completion(text, model=MODEL, usage=None):
    body = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


@pytest.fixture
def server():
    fake = FakeServer()
    fake.thread.start()
    yield fake
    fake.httpd.shutdown()
    fake.httpd.server_close()


def text_page(text="Hello") -> pymupdf.Page:
    doc = pymupdf.open()
    page = doc.new_page(width=144, height=72)
    page.insert_text((10, 40), text)
    return page


def pdf_bytes(pages: int) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        doc.new_page(width=144, height=72).insert_text((10, 40), f"page {i + 1}")
    return doc.tobytes()


def assert_no_secrets(text: str, server: FakeServer):
    assert API_KEY not in text
    assert server.base_url not in text
    assert "127.0.0.1" not in text
    assert str(server.httpd.server_port) not in text


# The request


def test_sends_the_page_image_with_the_fixed_prompt(server):
    result = read_page(text_page(), server.config())

    path, headers, body = server.requests[0]
    assert path == "/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert body["model"] == MODEL
    content = body["messages"][0]["content"]
    prompt = next(part["text"] for part in content if part["type"] == "text")
    image = next(part["image_url"]["url"] for part in content if part["type"] == "image_url")
    assert prompt == vision.PROMPT
    assert "Markdown" in prompt and "table" in prompt.lower()
    assert image.startswith("data:image/png;base64,")
    png = pymupdf.Pixmap(base64.b64decode(image.split(",", 1)[1]))
    # 144x72 pt rendered at the fixed DPI.
    assert (png.width, png.height) == (2 * vision.RENDER_DPI, vision.RENDER_DPI)
    assert result.number == 1


def test_prompt_is_the_same_for_every_page(server):
    data = pdf_bytes(2)

    read_pdf_pages(data, [1, 2], server.config())

    prompts = [
        [p["text"] for p in body["messages"][0]["content"] if p["type"] == "text"]
        for _, _, body in server.requests
    ]
    assert prompts == [[vision.PROMPT], [vision.PROMPT]]


def test_no_authorization_header_without_a_key(server):
    read_page(text_page(), server.config(api_key=None))

    assert "Authorization" not in server.requests[0][1]


def test_caller_may_pick_an_allowed_model(server):
    server.reply = lambda body: (200, completion("x", model=body["model"]))

    result = read_page(text_page(), server.config(), model="vision-large")

    assert server.requests[0][2]["model"] == "vision-large"
    assert result.model == "vision-large"


def test_a_model_outside_the_list_is_refused_before_any_request(server):
    with pytest.raises(ModelNotAllowedError):
        read_page(text_page(), server.config(), model="not-listed")

    assert server.requests == []


def test_an_unconfigured_backend_is_refused():
    with pytest.raises(VisionError) as info:
        read_image(b"png", 1, VisionConfig())

    assert info.value.code == "not_configured"


# The response


def test_returns_text_model_and_usage(server):
    usage = {"prompt_tokens": 812, "completion_tokens": 40, "total_tokens": 852}
    server.reply = lambda body: (200, completion("| a | b |\n|---|---|", usage=usage))

    result = read_page(text_page(), server.config())

    assert result == VisionResult(
        number=1, text="| a | b |\n|---|---|", model=MODEL, usage=usage
    )


def test_usage_is_none_when_the_endpoint_gives_none(server):
    result = read_page(text_page(), server.config())

    assert result.usage is None


def test_usage_keeps_only_numbers(server):
    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "prompt_tokens_details": {"cached_tokens": 0},
        "note": "free tier",
        "flag": True,
    }
    server.reply = lambda body: (200, completion("x", usage=usage))

    result = read_page(text_page(), server.config())

    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 2}


def test_model_name_is_the_one_the_endpoint_reports(server):
    server.reply = lambda body: (200, completion("x", model="vision-small-2026-01"))

    assert read_page(text_page(), server.config()).model == "vision-small-2026-01"


def test_model_name_falls_back_to_the_requested_model(server):
    reply = completion("x")
    del reply["model"]
    server.reply = lambda body: (200, reply)

    assert read_page(text_page(), server.config()).model == MODEL


def test_surrounding_markdown_fence_is_removed(server):
    server.reply = lambda body: (200, completion("```markdown\n# Title\n\nBody\n```\n"))

    assert read_page(text_page(), server.config()).text == "# Title\n\nBody"


# Errors are sanitized


@pytest.mark.parametrize(
    ("status", "payload", "code"),
    [
        (429, {"error": {"message": f"rate limited for key {API_KEY}"}}, "rate_limited"),
        (500, {"error": {"message": "boom"}}, "http_error"),
        (401, {"error": {"message": f"bad key {API_KEY}"}}, "http_error"),
        (200, b"not json", "bad_response"),
        (200, {"choices": []}, "bad_response"),
        (200, {"choices": [{"message": {"content": None}}]}, "bad_response"),
        (200, completion("   \n"), "empty_response"),
    ],
)
def test_endpoint_failures_raise_sanitized_errors(server, status, payload, code):
    server.reply = lambda body: (status, payload)

    with pytest.raises(VisionError) as info:
        read_page(text_page(), server.config())

    assert info.value.code == code
    if status != 200:
        assert info.value.status == status
    assert_no_secrets(str(info.value), server)
    assert_no_secrets(repr(info.value), server)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_timeout_raises_a_sanitized_error(server):
    server.delay = 0.5

    with pytest.raises(VisionError) as info:
        read_page(text_page(), server.config(timeout=0.1))

    assert info.value.code == "timeout"
    assert_no_secrets(str(info.value), server)


def test_unreachable_endpoint_raises_a_sanitized_error(server):
    config = server.config()
    server.httpd.shutdown()
    server.httpd.server_close()

    with pytest.raises(VisionError) as info:
        read_page(text_page(), config)

    assert info.value.code == "unreachable"
    assert_no_secrets(str(info.value), server)


def test_log_lines_never_contain_the_key_or_base_url(server, caplog):
    caplog.set_level(logging.DEBUG)
    server.reply = lambda body: (429, {"error": {"message": API_KEY}})

    with pytest.raises(VisionError):
        read_page(text_page(), server.config())
    server.reply = lambda body: (200, completion("ok"))
    read_page(text_page(), server.config())

    assert any(r.name == "llm_pdf_ocr.vision" for r in caplog.records)
    for record in caplog.records:
        assert_no_secrets(record.getMessage(), server)


# Several pages, in parallel


def test_reads_pages_in_the_order_given(server):
    server.reply = lambda body: (200, completion(f"call {len(server.requests)}"))

    results = read_pdf_pages(pdf_bytes(3), [3, 1], server.config())

    assert [r.number for r in results] == [3, 1]
    assert all(isinstance(r, VisionResult) for r in results)


@pytest.mark.parametrize("limit", [1, 2, 3])
def test_parallel_pages_never_exceed_the_configured_concurrency(server, limit):
    server.delay = 0.05

    results = read_pdf_pages(pdf_bytes(8), list(range(1, 9)), server.config(concurrency=limit))

    assert len(server.requests) == 8
    assert all(isinstance(r, VisionResult) for r in results)
    assert server.peak == limit


def test_concurrency_limit_holds_across_separate_calls(server):
    server.delay = 0.05
    config = server.config(concurrency=2)
    data = pdf_bytes(4)

    callers = [
        threading.Thread(target=read_pdf_pages, args=(data, [1, 2, 3, 4], config))
        for _ in range(3)
    ]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join()

    assert len(server.requests) == 12
    assert server.peak <= 2


def test_a_failed_page_does_not_stop_the_others(server):
    def reply(body):
        return (500, {}) if len(server.requests) == 2 else (200, completion("ok"))

    server.reply = reply

    results = read_pdf_pages(pdf_bytes(3), [1, 2, 3], server.config(concurrency=1))

    assert [type(r) for r in results] == [VisionResult, VisionError, VisionResult]
    assert results[1].number == 2


def test_a_page_outside_the_pdf_is_refused(server):
    with pytest.raises(ValueError, match="page 4"):
        read_pdf_pages(pdf_bytes(3), [4], server.config())

    assert server.requests == []
