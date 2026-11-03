"""Settings read from the environment: request limits, jobs, the web page and the vision backend.

The backend is off unless OCR_LLM_BASE_URL is set. Nothing here contacts the network.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_TIMEOUT = 60.0
DEFAULT_CONCURRENCY = 2
DEFAULT_MAX_FILE_MB = 50
DEFAULT_MAX_PAGES = 500
DEFAULT_PAGE_TIMEOUT = 120.0
DEFAULT_JOB_TTL = 3600.0
DEFAULT_MAX_JOBS = 20
_MiB = 1024 * 1024

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class ConfigError(ValueError):
    """An environment variable has a value that cannot be used."""


class ModelNotAllowedError(ValueError):
    """The requested model is not on the list a caller may pick from."""


@dataclass(frozen=True)
class Limits:
    """What one request may ask for: upload bytes, pages selected, seconds per OCR page."""

    max_file_bytes: int = DEFAULT_MAX_FILE_MB * _MiB
    max_pages: int = DEFAULT_MAX_PAGES
    page_timeout: float = DEFAULT_PAGE_TIMEOUT


@dataclass(frozen=True)
class JobSettings:
    """How long a finished job's result is kept, and how many jobs are held at once."""

    ttl: float = DEFAULT_JOB_TTL
    max_jobs: int = DEFAULT_MAX_JOBS


@dataclass(frozen=True)
class VisionConfig:
    base_url: str | None = None
    api_key: str | None = None
    default_model: str | None = None
    allowed_models: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    concurrency: int = DEFAULT_CONCURRENCY
    fallback_to_tesseract: bool = False

    @property
    def enabled(self) -> bool:
        return self.base_url is not None

    def resolve_model(self, requested: str | None) -> str:
        """Return the model to use, or raise if the caller may not pick it."""
        model = requested or self.default_model
        if model is None or model not in self.allowed_models:
            raise ModelNotAllowedError(f"model not allowed: {model!r}")
        return model


def _get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name, "").strip()
    return value or None


def _number(env: Mapping[str, str], name: str, default, cast):
    raw = _get(env, name)
    if raw is None:
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {raw!r}")
    return value


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def load_limits(env: Mapping[str, str] | None = None) -> Limits:
    """Read OCR_MAX_FILE_MB, OCR_MAX_PAGES and OCR_PAGE_TIMEOUT_SECONDS."""
    env = os.environ if env is None else env
    max_file_mb = _number(env, "OCR_MAX_FILE_MB", DEFAULT_MAX_FILE_MB, float)
    return Limits(
        max_file_bytes=int(max_file_mb * _MiB),
        max_pages=_number(env, "OCR_MAX_PAGES", DEFAULT_MAX_PAGES, int),
        page_timeout=_number(env, "OCR_PAGE_TIMEOUT_SECONDS", DEFAULT_PAGE_TIMEOUT, float),
    )


def load_job_settings(env: Mapping[str, str] | None = None) -> JobSettings:
    """Read OCR_JOB_TTL_SECONDS and OCR_MAX_JOBS."""
    env = os.environ if env is None else env
    return JobSettings(
        ttl=_number(env, "OCR_JOB_TTL_SECONDS", DEFAULT_JOB_TTL, float),
        max_jobs=_number(env, "OCR_MAX_JOBS", DEFAULT_MAX_JOBS, int),
    )


def load_web_page(env: Mapping[str, str] | None = None) -> bool:
    """Read OCR_WEB_PAGE: whether `/` serves the web page. On unless set to false."""
    env = os.environ if env is None else env
    return _get(env, "OCR_WEB_PAGE") is None or _flag(env, "OCR_WEB_PAGE")


def load_vision_config(env: Mapping[str, str] | None = None) -> VisionConfig:
    env = os.environ if env is None else env
    base_url = _get(env, "OCR_LLM_BASE_URL")
    default_model = _get(env, "OCR_LLM_DEFAULT_MODEL")
    if base_url is None:
        default_model = None
    elif default_model is None:
        raise ConfigError("OCR_LLM_DEFAULT_MODEL is required when OCR_LLM_BASE_URL is set")
    listed = (_get(env, "OCR_LLM_ALLOWED_MODELS") or "").split(",")
    allowed = [m.strip() for m in listed if m.strip()]
    if default_model:
        allowed.insert(0, default_model)
    return VisionConfig(
        base_url=base_url.rstrip("/") if base_url else None,
        api_key=_get(env, "OCR_LLM_API_KEY"),
        default_model=default_model,
        allowed_models=tuple(dict.fromkeys(allowed)) if base_url else (),
        timeout=_number(env, "OCR_LLM_TIMEOUT_SECONDS", DEFAULT_TIMEOUT, float),
        concurrency=_number(env, "OCR_LLM_CONCURRENCY", DEFAULT_CONCURRENCY, int),
        fallback_to_tesseract=_flag(env, "OCR_LLM_FALLBACK_TO_TESSERACT"),
    )
