"""Read pages with a vision model behind an OpenAI-compatible endpoint.

A page is rendered at a fixed DPI, its longer side capped, and sent to
`{base_url}/chat/completions` with a fixed prompt asking for Markdown with tables. The
answer is capped at `VisionConfig.max_tokens`, and no temperature is sent: greedy decoding
sends thinking models into long repetition. An answer cut off at the cap is an error, not
a page with its end missing. The reply's text, the model name and any usage numbers the
endpoint reports, reasoning tokens included, are returned and logged.

Requests in flight never exceed `VisionConfig.concurrency`, across all callers.
Errors and log lines never contain the API key, the base URL, or the endpoint's
own error text, which may echo either. A demo code's 429 is checked for litellm's
budget markers: a spent budget is `demo_exhausted`, not a transient rate limit.
"""

import base64
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pymupdf

from llm_pdf_ocr.config import VisionConfig
from llm_pdf_ocr.pdf import open_pdf

log = logging.getLogger(__name__)

# Enough for body text and table rules, without the payload size of 300 DPI.
RENDER_DPI = 150
# Keeps Letter and A4 at the full DPI, and a poster-sized page from becoming a huge image.
RENDER_MAX_SIDE = 2000

PROMPT = (
    "Transcribe this page image into Markdown. "
    "Keep the reading order, headings, lists and paragraphs. "
    "Write every table as a Markdown table. "
    "Return only the Markdown, with no commentary and no code fence."
)

# Finish reasons for an answer stopped at the cap; some endpoints spell it "max_tokens".
_CUT_OFF = ("length", "max_tokens")

_FENCE = re.compile(r"\A```[a-zA-Z]*\n(.*?)\n?```\Z", re.DOTALL)

# A Retry-After worth passing through: delta-seconds, as litellm sends it (it may be
# fractional). Anything else (an HTTP-date, or text echoing a credential) is dropped.
_RETRY_AFTER_SECONDS = re.compile(r"\d{1,6}(\.\d+)?")

# Only this much of a 429 body is read: litellm's error JSON is small, and the body is
# never shown or logged, only checked for its budget markers.
_ERROR_BODY_LIMIT = 65536


class VisionError(Exception):
    """The vision model could not read a page. The message is safe to show and log."""

    def __init__(
        self,
        code: str,
        message: str,
        number: int | None = None,
        status=None,
        usage=None,
        retry_after: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.number = number
        self.status = status
        self.usage = usage
        # The upstream Retry-After, only when it is a number of seconds; else None.
        self.retry_after = retry_after

    def __repr__(self) -> str:
        return f"VisionError(code={self.code!r}, message={str(self)!r}, number={self.number!r})"


@dataclass(frozen=True)
class VisionResult:
    """The Markdown of one page. `number` is 1-based; `usage` is None if not reported."""

    number: int
    text: str
    model: str
    usage: dict[str, int | float] | None = None
    finish_reason: str | None = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would forward the Authorization header to the new host."""

    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)

_limits: dict[tuple[str | None, int], threading.BoundedSemaphore] = {}
_limits_lock = threading.Lock()


def _limit(config: VisionConfig) -> threading.BoundedSemaphore:
    key = (config.base_url, config.concurrency)
    with _limits_lock:
        return _limits.setdefault(key, threading.BoundedSemaphore(config.concurrency))


def render_page(page: pymupdf.Page) -> bytes:
    """Render a page as a PNG at RENDER_DPI, its longer side capped at RENDER_MAX_SIDE."""
    longer = max(page.rect.width, page.rect.height)
    scale = min(RENDER_DPI / 72, RENDER_MAX_SIDE / longer)
    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")


def read_page(page: pymupdf.Page, config: VisionConfig, model: str | None = None) -> VisionResult:
    """Render one page and read it with the vision model."""
    return read_image(render_page(page), page.number + 1, config, model)


def read_image(
    png: bytes, number: int, config: VisionConfig, model: str | None = None
) -> VisionResult:
    """Read one rendered page. Raises ModelNotAllowedError or VisionError."""
    if not config.enabled:
        raise VisionError("not_configured", "the vision backend is not configured", number)
    model = config.resolve_model(model)
    with _limit(config):
        started = time.monotonic()
        try:
            result = _parse(_post(png, number, config, model), number, model, config)
        except VisionError as exc:
            log.warning(
                "page %d: vision model %s failed after %.2fs: %s (%s, cap %d)",
                number,
                model,
                time.monotonic() - started,
                exc.code,
                _tokens(exc.usage),
                config.max_tokens,
            )
            raise
        log.info(
            "page %d: read by %s in %.2fs (%d KB image, %s, finish %s)",
            number,
            model,
            time.monotonic() - started,
            len(png) // 1024,
            _tokens(result.usage),
            result.finish_reason or "?",
        )
    return result


def _tokens(usage: dict[str, int | float] | None) -> str:
    usage = usage or {}
    tokens_in = usage.get("prompt_tokens", "?")
    out = f"{tokens_in} tokens in, {usage.get('completion_tokens', '?')} tokens out"
    if "reasoning_tokens" in usage:
        out += f" ({usage['reasoning_tokens']} reasoning)"
    return out


def read_pdf_pages(
    data: bytes, numbers: list[int], config: VisionConfig, model: str | None = None
) -> list[VisionResult | VisionError]:
    """Read the given 1-based pages, in the order given, at most `concurrency` at once.

    A page that fails is returned as its VisionError, so the other pages still come back.
    """
    if not config.enabled:
        raise VisionError("not_configured", "the vision backend is not configured")
    config.resolve_model(model)
    with open_pdf(data) as doc:
        for number in numbers:
            if not 1 <= number <= len(doc):
                raise ValueError(f"page {number} is not in a {len(doc)}-page PDF")
        # Rendering stays on this thread: PyMuPDF documents are not thread-safe.
        images = [(number, render_page(doc[number - 1])) for number in numbers]

    def read(item: tuple[int, bytes]) -> VisionResult | VisionError:
        try:
            return read_image(item[1], item[0], config, model)
        except VisionError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=config.concurrency) as pool:
        return list(pool.map(read, images))


def _post(png: bytes, number: int, config: VisionConfig, model: str) -> bytes:
    image = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": PROMPT},
                    {"type": "image_url", "image_url": {"url": image}},
                ],
            }
        ],
        config.max_tokens_param: config.max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    request = urllib.request.Request(
        f"{config.base_url}/chat/completions",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    # Messages are written here, never copied from the exception or the endpoint's
    # body, and `from None` drops the original exception, which names the URL.
    try:
        with _opener.open(request, timeout=config.timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        retry_after = exc.headers.get("Retry-After", "").strip()
        raw = b""
        if status == 429:
            try:
                raw = exc.read(_ERROR_BODY_LIMIT)
            except (OSError, http.client.HTTPException):
                raw = b""
        exc.close()
        if status == 429:
            # litellm answers 429 for a spent budget and for an rpm/tpm limit alike;
            # only its budget markers tell them apart (the body is never shown or logged).
            if config.demo and _budget_exceeded(raw):
                raise VisionError(
                    "demo_exhausted", "this demo code is used up or expired", number, status
                ) from None
            raise VisionError(
                "rate_limited",
                "the vision model is rate limited (HTTP 429)",
                number,
                status,
                retry_after=(retry_after if _RETRY_AFTER_SECONDS.fullmatch(retry_after) else None),
            ) from None
        raise VisionError(
            "http_error", f"the vision model returned HTTP {status}", number, status
        ) from None
    except TimeoutError:
        raise _timeout(number, config) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise _timeout(number, config) from None
        raise VisionError("unreachable", "the vision model could not be reached", number) from None
    except OSError:
        raise VisionError("unreachable", "the vision model could not be reached", number) from None
    except http.client.HTTPException:
        raise VisionError(
            "bad_response", "the vision model sent an unreadable reply", number
        ) from None


def _timeout(number: int, config: VisionConfig) -> VisionError:
    return VisionError(
        "timeout", f"the vision model did not answer within {config.timeout:g} seconds", number
    )


def _budget_exceeded(raw: bytes) -> bool:
    """True when a demo gateway's 429 body names a spent budget rather than an rpm/tpm limit.

    Only litellm's budget markers are read: the `budget_exceeded` error type, or
    "Budget has been exceeded" in the message (case-insensitive). A rate limit carries
    neither. The body itself is never shown or logged: it may echo the demo code.
    """
    try:
        body = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    if str(error.get("type", "")).lower() == "budget_exceeded":
        return True
    return "budget has been exceeded" in str(error.get("message", "")).lower()


def _parse(raw: bytes, number: int, model: str, config: VisionConfig) -> VisionResult:
    try:
        reply = json.loads(raw)
        choice = reply["choices"][0]
        content = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        content = None
    if not isinstance(content, str):
        raise VisionError("bad_response", "the vision model sent an unreadable reply", number)
    finish_reason = choice.get("finish_reason")
    usage = _usage(reply.get("usage"))
    if finish_reason in _CUT_OFF:
        raise VisionError(
            "too_long",
            f"the vision model's answer was cut off at {config.max_tokens} tokens",
            number,
            usage=usage,
        )
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text:
        raise VisionError("empty_response", "the vision model returned no text", number)
    reported = reply.get("model")
    return VisionResult(
        number=number,
        text=text,
        model=reported if isinstance(reported, str) and reported else model,
        usage=usage,
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
    )


def _usage(usage) -> dict[str, int | float] | None:
    if not isinstance(usage, dict):
        return None
    numbers = {key: value for key, value in usage.items() if _is_number(value)}
    # OpenAI and OpenRouter count a thinking model's reasoning inside completion_tokens.
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and _is_number(details.get("reasoning_tokens")):
        numbers["reasoning_tokens"] = details["reasoning_tokens"]
    return numbers or None


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)
