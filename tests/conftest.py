import pytest

from llm_pdf_ocr.app import app, get_vision_config
from llm_pdf_ocr.config import VisionConfig


@pytest.fixture(autouse=True)
def no_vision_backend_from_the_environment():
    """Tests see no vision backend unless they configure one, whatever OCR_LLM_* is set to."""
    app.dependency_overrides[get_vision_config] = VisionConfig
    yield
    app.dependency_overrides.clear()
