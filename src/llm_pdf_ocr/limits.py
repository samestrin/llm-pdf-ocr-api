"""Enforce the upload size limit while the request body is read.

`UploadCap` counts body bytes as they arrive and stops at the cap, so an
oversized upload is never read whole: it is answered with 413 and the usual
error body. The cap allows `FORM_OVERHEAD` bytes on top of the file for the
multipart framing and form fields; `read_upload` then holds the file itself
to the exact limit.
"""

import json
from typing import BinaryIO

from starlette.types import ASGIApp, Message, Receive, Scope, Send

FORM_OVERHEAD = 64 * 1024


class FileTooLargeError(ValueError):
    """The uploaded file is larger than the limit."""

    code = "file_too_large"

    def __init__(self, limit: int):
        super().__init__(f"the file is larger than the limit of {limit} bytes")


class _BodyTooLarge(Exception):
    """Raised inside `receive` to stop the app reading any further."""


def read_upload(file: BinaryIO, limit: int) -> bytes:
    """Read at most `limit` bytes of an upload, or raise `FileTooLargeError`."""
    data = file.read(limit + 1)
    if len(data) > limit:
        raise FileTooLargeError(limit)
    return data


class UploadCap:
    """ASGI middleware: answer 413 once a body passes the file limit plus FORM_OVERHEAD.

    The limit is read per request from `app.state.limits`.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = scope["app"].state.limits.max_file_bytes
        cap = limit + FORM_OVERHEAD
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > cap:
            await _too_large(send, limit)
            return

        received = 0
        over = False
        started = False

        async def capped_receive() -> Message:
            nonlocal received, over
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    over = True
                    raise _BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if over:  # drop whatever the app says about the cut-off body
                return
            started = True
            await send(message)

        try:
            await self.app(scope, capped_receive, guarded_send)
        except _BodyTooLarge:
            pass
        if over and not started:
            await _too_large(send, limit)


async def _too_large(send: Send, limit: int) -> None:
    exc = FileTooLargeError(limit)
    body = json.dumps({"error": {"code": exc.code, "message": str(exc)}}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
