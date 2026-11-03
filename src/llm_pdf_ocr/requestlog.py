"""A request id on every response, and one structured log line per request.

`RequestLog` gives each request a fresh id (a client-supplied one is ignored, so
it cannot forge log lines), returns it in the `X-Request-ID` header and puts it in
every error body (`current_request_id`). It logs the method, path, status, time and
id, never the upload, the form fields or any page text. A failure nothing else
handled is logged by exception type only, and answered with 500 and the usual
error body, never a stack trace.
"""

import json
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    """The id of the request being served, or None outside one."""
    return _request_id.get()


class RequestLog:
    """ASGI middleware: set the request id, log the request, answer unhandled errors with 500."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        token = _request_id.set(request_id)
        started = time.monotonic()
        status = 0
        began = False

        async def tracked_send(message: Message) -> None:
            nonlocal status, began
            if message["type"] == "http.response.start":
                began = True
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                await self.app(scope, receive, tracked_send)
            except Exception as exc:  # the type is logged, never the message: it may hold text
                log.error(
                    "request_id=%s unhandled %s", request_id, type(exc).__name__, exc_info=False
                )
                if not began:
                    await _server_error(tracked_send, request_id)
        finally:
            log.info(
                "request_id=%s method=%s path=%s status=%d duration=%.3fs",
                request_id,
                scope["method"],
                scope["path"],
                status,
                time.monotonic() - started,
            )
            _request_id.reset(token)


async def _server_error(send: Send, request_id: str) -> None:
    body = json.dumps(
        {
            "error": {
                "code": "internal_error",
                "message": "the request could not be completed",
                "request_id": request_id,
            }
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
