"""Model presets a visitor may pick, and the vision config built from their key for one request.

The presets live in `presets.json`, beside this file: an id, a label, an OpenAI-compatible
base URL and a vision model each (and `max_tokens_param` where the provider does not read
`max_tokens`), plus `demo`, whose base URL is the internal
`DEMO_GATEWAY_URL` and whose key is a demo code the owner handed out.

A request names a preset by id in `X-Model-Preset` and sends its key in `X-Model-Key`, or a
demo code in `X-Demo-Code`. The id is looked up in that list and nothing else: a base URL is
never read from the request, so a caller cannot point the service at another address. The
key is used for that one request; it is never stored, logged, echoed or put in an error.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from llm_pdf_ocr.config import PublicDemoConfig, VisionConfig

PRESETS_FILE = Path(__file__).with_name("presets.json")

PRESET_HEADER = "X-Model-Preset"
KEY_HEADER = "X-Model-Key"
DEMO_CODE_HEADER = "X-Demo-Code"
DEMO = "demo"
# The one internal address a preset may name; its value comes from PublicDemoConfig.
DEMO_GATEWAY_ENV = "DEMO_GATEWAY_URL"
MAX_KEY_LENGTH = 512
MAX_TOKENS_PARAMS = ("max_tokens", "max_completion_tokens")

_ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    vision_model: str
    base_url: str | None = None
    base_url_env: str | None = None
    max_tokens_param: str = "max_tokens"


class CredentialsError(ValueError):
    """The request's preset or key cannot be used. The message never holds the key."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def __repr__(self) -> str:
        return f"CredentialsError(code={self.code!r}, message={str(self)!r})"


def load_presets(path: Path | None = None) -> tuple[Preset, ...]:
    """The presets in `presets.json`, or in `path`. Raises ValueError on a bad file."""
    return _read(path or PRESETS_FILE)


@cache
def _read(path: Path) -> tuple[Preset, ...]:
    presets: list[Preset] = []
    for entry in json.loads(path.read_text(encoding="utf-8")):
        preset_id = entry.get("id")
        if not preset_id or not isinstance(preset_id, str) or set(preset_id) - _ID_CHARS:
            raise ValueError(f"preset id must be lowercase letters, digits and -: {preset_id!r}")
        if any(p.id == preset_id for p in presets):
            raise ValueError(f"preset {preset_id!r} is listed twice")
        for name in ("label", "vision_model"):
            if not isinstance(entry.get(name), str) or not entry[name]:
                raise ValueError(f"preset {preset_id!r} has no {name}")
        base_url, base_url_env = entry.get("base_url"), entry.get("base_url_env")
        if base_url_env is not None:
            if base_url is not None or base_url_env != DEMO_GATEWAY_ENV:
                raise ValueError(
                    f"preset {preset_id!r}: base_url_env may only be {DEMO_GATEWAY_ENV}"
                )
        elif not isinstance(base_url, str) or not base_url:
            raise ValueError(f"preset {preset_id!r} needs a base_url")
        elif not base_url.startswith("https://"):
            raise ValueError(f"preset {preset_id!r}: base_url must be https")
        max_tokens_param = entry.get("max_tokens_param", "max_tokens")
        if max_tokens_param not in MAX_TOKENS_PARAMS:
            raise ValueError(
                f"preset {preset_id!r}: max_tokens_param must be one of {MAX_TOKENS_PARAMS}"
            )
        presets.append(
            Preset(
                id=preset_id,
                label=entry["label"],
                vision_model=entry["vision_model"],
                base_url=base_url.rstrip("/") if base_url else None,
                base_url_env=base_url_env,
                max_tokens_param=max_tokens_param,
            )
        )
    return tuple(presets)


def public_presets() -> list[dict[str, str]]:
    """Each preset's id, label and vision model, for a caller to choose from. No base URL."""
    return [{"id": p.id, "label": p.label, "vision_model": p.vision_model} for p in load_presets()]


def request_vision_config(
    headers: Mapping[str, str], server: VisionConfig, demo: PublicDemoConfig
) -> VisionConfig:
    """The vision config for one request, from its credential headers.

    With none of the headers, the server's own config is returned unchanged (off when no
    vision backend is configured). Otherwise the preset decides the base URL, model and
    token-cap field, and the key or demo code is the API key; timeout, concurrency and the
    token cap stay the server's.
    Raises CredentialsError.
    """
    sent = {name.lower(): value.strip() for name, value in headers.items()}
    preset_id = sent.get(PRESET_HEADER.lower(), "").lower()
    key = sent.get(KEY_HEADER.lower(), "")
    code = sent.get(DEMO_CODE_HEADER.lower(), "")
    if not (preset_id or key or code):
        return server
    if not preset_id and code:
        preset_id = DEMO
    presets = {p.id: p for p in load_presets()}
    if not demo.demo_available:
        presets.pop(DEMO, None)
    offered = ", ".join(presets)
    if not preset_id:
        raise CredentialsError(
            "preset_not_allowed", f"{KEY_HEADER} needs {PRESET_HEADER}, one of: {offered}"
        )
    preset = presets.get(preset_id)
    if preset is None:
        # The value is not echoed: it is the caller's text and may hold anything.
        if preset_id == DEMO:
            message = "demo codes are not available on this service"
        else:
            message = f"{PRESET_HEADER} is not a preset this service offers; use one of: {offered}"
        raise CredentialsError("preset_not_allowed", message)
    header, secret = (DEMO_CODE_HEADER, code) if preset.id == DEMO else (KEY_HEADER, key)
    if not secret:
        raise CredentialsError(
            "credentials_required", f"the {preset.label} preset needs {header}", 401
        )
    if len(secret) > MAX_KEY_LENGTH or not all("!" <= c <= "~" for c in secret):
        raise CredentialsError("invalid_key", f"{header} is not a valid key")
    return VisionConfig(
        base_url=preset.base_url if preset.base_url_env is None else demo.gateway_url,
        api_key=secret,
        default_model=preset.vision_model,
        allowed_models=(preset.vision_model,),
        timeout=server.timeout,
        concurrency=server.concurrency,
        max_tokens=server.max_tokens,
        max_tokens_param=preset.max_tokens_param,
    )
