import fitz  # PyMuPDF
from transformers import TrOCRProcessor, VisionEncoderDecoderModel

processor = TrOCRProcessor.from_pretrained("microsoft/trocr-base-handwritten")
model = VisionEncoderDecoderModel.from_pretrained("microsoft/trocr-base-handwritten")

def extract_text(file_stream):
    doc = fitz.open(stream=file_stream.read(), filetype="pdf")
    text = ""
    for page in doc:
        img = page.get_pixmap()
        img_bytes = img.tobytes()
        inputs = processor(images=img_bytes, return_tensors="pt")
        outputs = model.generate(**inputs)
        text += processor.batch_decode(outputs, skip_special_tokens=True)[0] + "\n"
    return text

def get_supported_languages():
    return ["English"]  # Extend this list based on the actual model's capabilities
