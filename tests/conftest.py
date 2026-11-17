import pytest

from llm_pdf_ocr.app import app, get_public_demo_config, get_vision_config
from llm_pdf_ocr.config import PublicDemoConfig, VisionConfig


@pytest.fixture(autouse=True)
def no_vision_backend_from_the_environment():
    """Tests see no vision backend and no demo gateway unless they configure one, whatever
    OCR_LLM_*, DEMO_GATEWAY_URL or PUBLIC_DEMO_NOTICE is set to."""
    app.dependency_overrides[get_vision_config] = VisionConfig
    app.dependency_overrides[get_public_demo_config] = PublicDemoConfig
    yield
    app.dependency_overrides.clear()
