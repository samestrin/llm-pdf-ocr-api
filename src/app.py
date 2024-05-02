import fitz  # PyMuPDF
import sentencepiece as spm
import io

from flask import Flask, request, jsonify
from transformers import TrOCRProcessor, VisionEncoderDecoderModel
from PIL import Image

# Load default model
default_model_name = "microsoft/trocr-base-printed"
processor = TrOCRProcessor.from_pretrained(default_model_name)
model = VisionEncoderDecoderModel.from_pretrained(default_model_name)

def create_app():
    app = Flask(__name__)

    @app.route('/ocr', methods=['POST'])
    def ocr():
        file = request.files['file']
        model_name = request.form.get('model', None)
        try:
            text = extract_text(file, model_name=model_name)
            return jsonify({'text': text})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/models', methods=['GET'])
    def list_models():
        models = get_supported_models()
        return jsonify({'supported_models': models})

    @app.errorhandler(400)
    def bad_request(error):
        return jsonify({'error': 'Bad request'}), 400

    @app.errorhandler(500)
    def internal_server_error(error):
        return jsonify({'error': 'Internal server error'}), 500

    return app

def extract_text(file_stream, model_name=None):
    global processor, model
    if model_name and model_name != default_model_name:
        try:
            processor = TrOCRProcessor.from_pretrained(model_name)
            model = VisionEncoderDecoderModel.from_pretrained(model_name)
        except Exception as e:
            raise Exception(f"Error loading model: {e}")

    try:
        doc = fitz.open(stream=file_stream.read(), filetype="pdf")
        text = ""
        for page in doc:
            img = page.get_pixmap()
            img_bytes = img.tobytes()
            image = Image.open(io.BytesIO(img_bytes))
            inputs = processor(images=image, return_tensors="pt")
            outputs = model.generate(**inputs)
            text += processor.batch_decode(outputs, skip_special_tokens=True)[0] + "\n"
    except Exception as e:
        raise Exception(f"Error processing document: {e}")

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

app = create_app()
