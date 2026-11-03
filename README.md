# llm-pdf-ocr-api

[![Star on GitHub](https://img.shields.io/github/stars/samestrin/llm-pdf-ocr-api?style=social)](https://github.com/samestrin/llm-pdf-ocr-api/stargazers)[![Fork on GitHub](https://img.shields.io/github/forks/samestrin/llm-pdf-ocr-api?style=social) ](https://github.com/samestrin/llm-pdf-ocr-api/network/members)[![Watch on GitHub](https://img.shields.io/github/watchers/samestrin/llm-pdf-ocr-api?style=social)](https://github.com/samestrin/llm-pdf-ocr-api/watchers)

![Version 0.0.1](https://img.shields.io/badge/Version-0.0.1-blue) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg) ](https://opensource.org/licenses/MIT)[![Built with Python](https://img.shields.io/badge/Built%20with-Python-green)](https://www.python.org/)

**llm-pdf-ocr-api** is a Flask-based web service designed to perform Optical Character Recognition (OCR) on PDF files using machine vision and AI models. Built on PyTorch and Transformers and optimized with NVIDIA CUDA, this API provides an endpoint for OCR processing. This API is wrapped in a Docker container.

### OCR Process Overview

When a user submits a file to the /ocr endpoint, the following steps are executed:

1.  **Receive the Request:**
    - The server accepts a POST request containing the PDF file and optional parameters for OCR settings.
2.  **Extract and Open the PDF:**
    - The PDF file is extracted from the form data and opened to access its content.
3.  **Configure OCR Parameters:**
    - Parameters for the OCR process, such as the model and image processing settings, are set with defaults applied where not specified.
      - Optional parameters are read from the form data, such as `model`, `threshold_value`, `kernel_width`, `kernel_height`, and `min_area`.
      - Defaults are used for any parameters not provided.
4.  **Process Each Page:**
    - Each page of the PDF is processed sequentially. The steps include:
      - Rendering the page as an image.
      - Converting the image to grayscale and applying binary thresholding.
      - Performing morphological operations to enhance image clarity.
      - Extracting lines using contour detection and filtering by area.
5.  **Extract Text:**
    - Text is extracted from each line of the image using the TrOCR model. The text from all lines is compiled into a single output.
6.  **Return the Response:**
    - The extracted text is sent back in a JSON response.
7.  **Handle Errors:**
    - Errors during processing are caught and returned as a detailed error message.

## Dependencies

- **Python**: The script runs in a Python3 environment.
- **Flask**: Serves as the backbone of the web application, facilitating the creation of endpoints and handling HTTP requests.
- **google-protobuf**: Utilized for data serialization and deserialization, important for model loading and configuration.
- **gunicorn**: An extension that provides a Python WSGI HTTP Server for UNIX.
- **numpy**: Supports high-performance operations on large multi-dimensional arrays and matrices, used extensively in image manipulation.
- **OpenCV (opencv-python-headless)**: Used to segment larger bodies of text into individual lines.
- **Pillow (PIL)**: Helps with image processing tasks through the Python Imaging Library (Fork).
- **PyMuPDF (fitz)**: Utilized for PDF parsing with Python bindings for the MuPDF library.
- **sentencepiece**: Helps with unsupervised text tokenization and detokenization.
- **torch**: Utilized for machine learning tasks in computer vision and natural language processing.
- **transformers**: State-of-the-art Natural Language Processing for TensorFlow 2.0 and PyTorch.

### Installation

To install llm-pdf-ocr-api, follow these steps:

Begin by cloning the repository containing the llm-newsletter-generator to your local machine.

```bash
git clone https://github.com/samestrin/llm-pdf-ocr-api/
```

Navigate to the project directory:

```bash
cd llm-pdf-ocr-api
```

Install the required dependencies using [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

## Endpoints

Interactive OpenAPI docs are served at `/docs` (schema at `/openapi.json`).

### Extract

**Endpoint:** `/v1/extract` **Method:** POST

Read a PDF page by page and return Markdown or JSON. Each page says how it was read: `text_layer` (the PDF's own text), `tesseract` (OCR, with a 0 to 1 confidence) or `vision` (a vision model, named in `model`).

- `file`: PDF file
- `format` (optional): `markdown` (default) or `json`.
- `pages` (optional): 1-based pages to read, such as `1-3,5`. Defaults to every page.
- `method` (optional): `auto` (default) uses the text layer when it is usable, else the vision model if the operator configured one, else Tesseract. `text_layer`, `tesseract` or `vision` reads every page that way.
- `model` (optional): the vision model to use, from the operator's `OCR_LLM_ALLOWED_MODELS`. Defaults to `OCR_LLM_DEFAULT_MODEL`. Any other model is refused with 400 `model_not_allowed`, and `method=vision` with no vision model configured is refused with 400 `vision_not_configured`.

```bash
curl -F file=@document.pdf -F format=json -F pages=1-2 localhost:5000/v1/extract
```

A page that could not be read is returned as `failed`, with an error code and message, and is listed in `failed_pages`; the other pages are still returned.

### OCR (deprecated)

**Endpoint:** `/ocr` **Method:** POST

Kept for users of 0.0.1. Returns `{"text": ..., "failed_pages": [...]}`: the text of every page that was read, and the pages that failed. The 0.0.1 `model`, `threshold_value`, `kernel_width`, `kernel_height` and `min_area` fields are ignored, and no model is downloaded. Pages are routed as `method=auto` on `/v1/extract`. Use `/v1/extract` instead.

- `file`: PDF file

### Health

**Endpoint:** `/health` **Method:** GET

Returns `{"status": "ok"}`.

## Error Handling

A request that cannot be served returns `{"error": {"code": ..., "message": ...}}`, with no stack trace:

- **400 Bad Request**: `invalid_pdf` (not a readable PDF, or it has no pages), `encrypted_pdf`, `invalid_page_range` or `page_out_of_range`.
- **422 Unprocessable Entity**: a missing `file` or an unknown `format`.

## Contribute

Contributions to this project are welcome. Please fork the repository and submit a pull request with your changes or improvements.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Share

[![Twitter](https://img.shields.io/badge/X-Tweet-blue)](https://twitter.com/intent/tweet?text=Check%20out%20this%20awesome%20project!&url=https://github.com/samestrin/llm-pdf-ocr-api) [![Facebook](https://img.shields.io/badge/Facebook-Share-blue)](https://www.facebook.com/sharer/sharer.php?u=https://github.com/samestrin/llm-pdf-ocr-api) [![LinkedIn](https://img.shields.io/badge/LinkedIn-Share-blue)](https://www.linkedin.com/sharing/share-offsite/?url=https://github.com/samestrin/llm-pdf-ocr-api)
