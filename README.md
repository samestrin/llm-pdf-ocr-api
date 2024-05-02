# llm-pdf-ocr-api-digitalocean

[![Star on GitHub](https://img.shields.io/github/stars/samestrin/llm-pdf-ocr-api-digitalocean?style=social)](https://github.com/samestrin/llm-pdf-ocr-api-digitalocean/stargazers)[![Fork on GitHub](https://img.shields.io/github/forks/samestrin/llm-pdf-ocr-api-digitalocean?style=social) ](https://github.com/samestrin/llm-pdf-ocr-api-digitalocean/network/members)[![Watch on GitHub](https://img.shields.io/github/watchers/samestrin/llm-pdf-ocr-api-digitalocean?style=social)](https://github.com/samestrin/llm-pdf-ocr-api-digitalocean/watchers)

![Version 0.0.1](https://img.shields.io/badge/Version-0.0.1-blue) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg) ](https://opensource.org/licenses/MIT)[![Built with Python](https://img.shields.io/badge/Built%20with-Python-green)](https://www.python.org/)

The llm-pdf-ocr-api-digitalocean is a Flask-based web service designed to perform OCR on PDF files. It provides an endpoint for OCR processing, and listing models.

_This is under active development._

## Dependencies

- **Flask**: Used for creating the web server and handling the REST API HTTP requests.
- **PyMuPDF**: Handles PDF files.
- **transformers**: Utilized for OCR capabilities using models from Hugging Face.
- **torch**: Required for running the models from transformers.

## Deploy to DigitalOcean App Platform

Click this button to deploy the project to your Digital Ocean account:

[![Deploy to DO](https://www.deploytodo.com/do-btn-blue.svg)](https://cloud.digitalocean.com/apps/new?repo=https://github.com/samestrin/llm-pdf-ocr-api-digitalocean/tree/main&refcode=2d3f5d7c5fbe)

### Installation

To install llm-pdf-ocr-api-digitalocean, follow these steps:

```bash
git clone https://github.com/samestrin/llm-pdf-ocr-api-digitalocean/
```

```bash
cd llm-pdf-ocr-api-digitalocean
```

Install the required dependencies using pip:

```bash
pip install -r src/requirements.txt
```

## Endpoints

### OCR Process

**Endpoint:** `/ocr` **Method:** POST

Process a PDF file and return the extracted text.

- `file`: PDF file
- `model`: Model name (optional) - defaults to [microsoft/trocr-base-printed](https://huggingface.co/microsoft/trocr-base-printed)

**Endpoint:** `/models` **Method:** GET

Show all AI models available.

## Error Handling

The API handles errors gracefully and returns appropriate error responses:

- **400 Bad Request**: Invalid request parameters.
- **500 Internal Server Error**: Unexpected server error.

## Contribute

Contributions to this project are welcome. Please fork the repository and submit a pull request with your changes or improvements.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Share

[![Twitter](https://img.shields.io/badge/X-Tweet-blue)](https://twitter.com/intent/tweet?text=Check%20out%20this%20awesome%20project!&url=https://github.com/samestrin/llm-pdf-ocr-api-digitalocean) [![Facebook](https://img.shields.io/badge/Facebook-Share-blue)](https://www.facebook.com/sharer/sharer.php?u=https://github.com/samestrin/llm-pdf-ocr-api-digitalocean) [![LinkedIn](https://img.shields.io/badge/LinkedIn-Share-blue)](https://www.linkedin.com/sharing/share-offsite/?url=https://github.com/samestrin/llm-pdf-ocr-api-digitalocean)
