"""
llm-pdf-ocr-api-digitalocean is a Flask-based web service designed to perform Optical Character 
Recognition (OCR) on PDF files using machine vision and AI models. Built on PyTorch and Transformers, 
this API provides two endpoints, one for OCR processing, and one for listing available models.

Copyright (c) 2024-PRESENT Sam Estrin
This script is licensed under the MIT License (see LICENSE for details)
GitHub: https://github.com/samestrin/llm-pdf-ocr-api-digitalocean
"""

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
    """Creates and configures an instance of a Flask application.

    Returns:
        Flask: The Flask application instance.
    """    
    app = Flask(__name__)

    @app.route('/ocr', methods=['POST'])
    def ocr():
        """Handles OCR processing for uploaded files through POST requests.

        Retrieves file and OCR settings from the request, processes the file to extract text,
        and returns the extracted text or an error message in JSON format.

        Returns:
            Response: JSON response containing extracted text or error message.
        """        
        file = request.files['file']
        model_name = request.form.get('model', default_model_name)
        
        # Parameters for segment_lines
        threshold_value = int(request.form.get('threshold_value', 150))
        kernel_width = int(request.form.get('kernel_width', 20))
        kernel_height = int(request.form.get('kernel_height', 1))
        min_area = int(request.form.get('min_area', 50))

        try:
            text = extract_text(file, model_name=model_name, threshold_value=threshold_value,
                                kernel_width=kernel_width, kernel_height=kernel_height, min_area=min_area)
            return jsonify({'text': text})
        except Exception as e:
            return internal_server_error(e)


    @app.route('/models', methods=['GET'])
    def list_models():
        """Provides a list of supported OCR models via a GET request.

        Returns:
            Response: JSON response containing a list of supported OCR models.
        """        
        models = get_supported_models()
        return jsonify({'supported_models': models})

    @app.errorhandler(400)
    def bad_request(error):
        """Handles HTTP 400 errors by returning a JSON formatted bad request message along with error details.

        Args:
            error: The error object provided by Flask.

        Returns:
            Response: JSON response indicating a bad request and including the error description.
        """      
        return jsonify({'error': 'Bad request', 'details': str(error)}), 400


    @app.errorhandler(500)
    def internal_server_error(error):
        """Handles HTTP 500 errors by returning a JSON formatted internal server error message along with error details.

        Args:
            error: The error object provided by Flask.

        Returns:
            Response: JSON response indicating an internal server error and including the error description.
        """      
        return jsonify({'error': 'Internal server error', 'details': str(error)}), 500
    
    return app

def segment_lines(image, threshold_value=150, kernel_width=20, kernel_height=1, min_area=50):
    """Segments an image into lines based on provided image processing parameters.

    Args:
        image (Image): The image to process.
        threshold_value (int): Value for thresholding operation.
        kernel_width (int): Width of the kernel for morphological operations.
        kernel_height (int): Height of the kernel for morphological operations.
        min_area (int): Minimum area to consider a contour as a line.

    Returns:
        list: A list of cropped images, each containing a line of text.
    """    
    gray = cv2.cvtColor(np.array(image), cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, threshold_value, 255, cv2.THRESH_BINARY_INV)
    
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_width, kernel_height))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)
    
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    lines = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area > min_area:
            x, y, w, h = cv2.boundingRect(cnt)
            line = image.crop((x, y, x + w, y + h))
            lines.append(line)
    return lines


def extract_text(file_stream, model_name=None, threshold_value=150, kernel_width=20, kernel_height=1, min_area=50):
    """Extracts text from a PDF file using OCR, configurable via POST parameters.

    Args:
        file_stream (io.BytesIO): The file stream of the PDF.
        model_name (str): The model name for the OCR processor, defaults to a pre-set model.
        threshold_value (int), kernel_width (int), kernel_height (int), min_area (int):
            Parameters forwarded to the segment_lines function for image processing.

    Returns:
        str: Extracted text from the PDF document.
    """    
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

        debug_dir = "debug_line_images"
        os.makedirs(debug_dir, exist_ok=True)
        line_counter = 0

        for page_number, page in enumerate(doc):
            img = page.get_pixmap()
            img_bytes = img.tobytes()
            image = Image.open(io.BytesIO(img_bytes))
            lines = segment_lines(image, threshold_value, kernel_width, kernel_height, min_area)
            for line in lines:
                line_image_path = os.path.join(debug_dir, f"line_{page_number}_{line_counter}.png")
                line.save(line_image_path)
                line_counter += 1

                inputs = processor(images=line, return_tensors="pt").to(device)
                outputs = model.generate(**inputs)
                text += processor.batch_decode(outputs, skip_special_tokens=True)[0] + "\n"
    except Exception as e:
        raise Exception(f"Error processing document: {e}")

    return text

def get_supported_models():
    """Lists all OCR models supported by the application.

    Returns:
        list: A list of supported model identifiers.
    """    
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
