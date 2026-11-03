"""Helpers for comparing response bodies."""


def without_request_id(response) -> dict:
    """The JSON body, with the per-request id dropped from an error so it can be compared whole."""
    body = response.json()
    if isinstance(body.get("error"), dict):
        body["error"].pop("request_id", None)
    return body
