import fitz  # PyMuPDF
import sentencepiece as spm
import io
import cv2
import numpy as np
import torch
import os
import google.protobuf

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

def segment_lines(image):
    # Convert to grayscale
    gray = cv2.cvtColor(np.array(image), cv2.COLOR_BGR2GRAY)
    
    # Apply adaptive thresholding
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 11, 2)
    
    # Use morphological operations to close gaps in between lines of text
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (20, 1))  # Adjust the kernel size as needed
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    
    # Find contours
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    lines = []
    min_area = 50  # Adjust this value based on expected smallest area of text line
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area > min_area:
            x, y, w, h = cv2.boundingRect(cnt)
            line = image.crop((x, y, x + w, y + h))
            lines.append(line)
    return lines


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
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
        model = model.to(device)

        # Create a directory to store the line images for debugging
        debug_dir = "debug_line_images"
        os.makedirs(debug_dir, exist_ok=True)
        line_counter = 0

        for page_number, page in enumerate(doc):
            img = page.get_pixmap()
            img_bytes = img.tobytes()
            image = Image.open(io.BytesIO(img_bytes))
            lines = segment_lines(image)
            for line in lines:
                # Save each line image for debugging
                line_image_path = os.path.join(debug_dir, f"line_{page_number}_{line_counter}.png")
                line.save(line_image_path)
                line_counter += 1

                # Continue with OCR processing                
                inputs = processor(images=line, return_tensors="pt").to(device)
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
