import pytest

from llm_pdf_ocr.config import (
    ConfigError,
    ModelNotAllowedError,
    VisionConfig,
    load_vision_config,
    load_web_page,
)

FULL = {
    "OCR_LLM_BASE_URL": "http://llm.local:8000/v1/",
    "OCR_LLM_API_KEY": "secret",
    "OCR_LLM_DEFAULT_MODEL": "vision-a",
    "OCR_LLM_ALLOWED_MODELS": "vision-b, vision-c",
    "OCR_LLM_TIMEOUT_SECONDS": "12.5",
    "OCR_LLM_CONCURRENCY": "3",
    "OCR_LLM_FALLBACK_TO_TESSERACT": "true",
}


def test_unset_is_off_with_safe_defaults():
    config = load_vision_config({})
    assert config.enabled is False
    assert config.fallback_to_tesseract is False
    assert config.base_url is None
    assert config.api_key is None


def test_blank_values_count_as_unset():
    assert load_vision_config({"OCR_LLM_BASE_URL": "  "}).enabled is False


def test_set_reads_every_field():
    config = load_vision_config(FULL)
    assert config.enabled is True
    assert config.base_url == "http://llm.local:8000/v1"
    assert config.api_key == "secret"
    assert config.default_model == "vision-a"
    assert config.allowed_models == ("vision-a", "vision-b", "vision-c")
    assert config.timeout == 12.5
    assert config.concurrency == 3
    assert config.fallback_to_tesseract is True


def test_defaults_for_timeout_and_concurrency():
    config = load_vision_config({"OCR_LLM_BASE_URL": "http://x", "OCR_LLM_DEFAULT_MODEL": "m"})
    assert config.timeout == 60.0
    assert config.concurrency == 2
    assert config.allowed_models == ("m",)


def test_base_url_without_default_model_is_an_error():
    with pytest.raises(ConfigError, match="OCR_LLM_DEFAULT_MODEL"):
        load_vision_config({"OCR_LLM_BASE_URL": "http://x"})


@pytest.mark.parametrize(
    "name,value",
    [
        ("OCR_LLM_TIMEOUT_SECONDS", "abc"),
        ("OCR_LLM_TIMEOUT_SECONDS", "0"),
        ("OCR_LLM_CONCURRENCY", "1.5"),
        ("OCR_LLM_CONCURRENCY", "0"),
        ("OCR_LLM_FALLBACK_TO_TESSERACT", "maybe"),
    ],
)
def test_bad_values_raise(name, value):
    with pytest.raises(ConfigError, match=name):
        load_vision_config({**FULL, name: value})


@pytest.mark.parametrize("value", ["1", "yes", "ON", "True"])
def test_fallback_truthy(value):
    assert load_vision_config(
        {**FULL, "OCR_LLM_FALLBACK_TO_TESSERACT": value}
    ).fallback_to_tesseract


@pytest.mark.parametrize("value", ["0", "no", "off", "false", ""])
def test_fallback_falsy(value):
    config = load_vision_config({**FULL, "OCR_LLM_FALLBACK_TO_TESSERACT": value})
    assert config.fallback_to_tesseract is False


def test_resolve_model_default_and_allowed():
    config = load_vision_config(FULL)
    assert config.resolve_model(None) == "vision-a"
    assert config.resolve_model("vision-c") == "vision-c"


def test_disallowed_model_is_rejected():
    config = load_vision_config(FULL)
    with pytest.raises(ModelNotAllowedError, match="other"):
        config.resolve_model("other")


def test_resolve_model_when_off_is_rejected():
    assert isinstance(load_vision_config({}), VisionConfig)
    with pytest.raises(ModelNotAllowedError):
        load_vision_config({}).resolve_model("x")


def test_other_settings_without_base_url_stay_off():
    config = load_vision_config({**FULL, "OCR_LLM_BASE_URL": ""})
    assert config.enabled is False
    assert config.default_model is None
    assert config.allowed_models == ()
    with pytest.raises(ModelNotAllowedError):
        config.resolve_model("vision-b")


def test_web_page_is_on_with_nothing_configured():
    assert load_web_page({}) is True
    assert load_web_page({"OCR_WEB_PAGE": "  "}) is True


@pytest.mark.parametrize("value", ["false", "0", "no", "OFF"])
def test_web_page_can_be_turned_off(value):
    assert load_web_page({"OCR_WEB_PAGE": value}) is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "On"])
def test_web_page_can_be_turned_on(value):
    assert load_web_page({"OCR_WEB_PAGE": value}) is True


def test_web_page_rejects_an_unknown_value():
    with pytest.raises(ConfigError, match="OCR_WEB_PAGE"):
        load_web_page({"OCR_WEB_PAGE": "maybe"})
