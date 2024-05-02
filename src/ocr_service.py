import fitz  # PyMuPDF
from transformers import TrOCRProcessor, VisionEncoderDecoderModel

# Load default models
default_model_name = "microsoft/trocr-base-printed"
processor = TrOCRProcessor.from_pretrained(default_model_name)
model = VisionEncoderDecoderModel.from_pretrained(default_model_name)

def extract_text(file_stream, model_name=None):
    global default_model_name, processor, model
    # Check if a different model is requested, note this is resources intensive!
    if model_name and model_name != default_model_name:
        processor = TrOCRProcessor.from_pretrained(model_name)
        model = VisionEncoderDecoderModel.from_pretrained(model_name)

    doc = fitz.open(stream=file_stream.read(), filetype="pdf")
    text = ""
    for page in doc:
        img = page.get_pixmap()
        img_bytes = img.tobytes()
        inputs = processor(images=img_bytes, return_tensors="pt")
        outputs = model.generate(**inputs)
        text += processor.batch_decode(outputs, skip_special_tokens=True)[0] + "\n"
    return text

def get_supported_models():
    return [
        "microsoft/trocr-large-handwritten",
        "microsoft/trocr-large-printed",
        "microsoft/trocr-small-printed",
        "microsoft/trocr-small-handwritten",
        "microsoft/trocr-base-handwritten",
        "microsoft/trocr-base-printed",
        "microsoft/trocr-base-stage1",
        "microsoft/trocr-large-stage1"
    ]
