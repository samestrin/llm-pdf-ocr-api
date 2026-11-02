# Vision

`llm-pdf-ocr-api` exists so that anyone can turn a PDF, scanned or not, into clean text they can use, with one self-hosted service and no vendor lock-in.
It serves developers who need PDF text in their own apps and pipelines, and the people who just want to drop a file into a page and get the text back.
It turns a PDF into Markdown or JSON, page by page, with tables kept as tables.
It owns exactly one thing: getting the text out of PDFs well.

## Works with nothing configured

A plain `docker run` works on a laptop with no GPU, no API key and no model download.
A page with a usable text layer is read directly, which is fast, exact and free.
A scanned page with no vision model configured is read with Tesseract on the CPU.
The service never requires a GPU, a cloud account or a specific vendor.

## A vision model is optional and pluggable

When a vision model is configured, scanned pages and hard layouts go to it for better results.
The model is any OpenAI-compatible endpoint, local or cloud, set from the environment.
No vendor is special, and the service ships no model inside the image.
A caller can see which method read each page (text layer, Tesseract or the vision model), which model was used, and the cost signals the endpoint returns.
The caller chooses the model only from a list the operator allows; an arbitrary model name from a request is refused.

## Nothing reports success when it did not succeed

This rule sits above every other rule here.
A page that failed is reported as failed, never as an empty page.
A partial result says which pages are missing and why.
A timeout, a model error and a bad PDF each have their own error, with no stack trace sent to the caller.

## Safe to run where others can reach it

Uploads have limits on size and page count, set by the operator.
Files are processed and dropped; nothing is stored unless the operator turns on storage.
Encrypted, malformed and oversized PDFs are rejected cleanly.

## Measured, not claimed

Quality claims in the README come from a small committed benchmark of real documents, per method.
Every change to how pages are read is checked against that benchmark.
The API is described by OpenAPI, and the docs match what the service does.

## Scope

It is not a document management system, a search engine, a translator, a summarizer or a general file converter.
It does not chat with documents and does not call a model to rewrite the text it extracts.
It ships a small local web page for trying it out; a hosted public demo is a separate, optional deployment with stricter limits.
It is Python, built on the PDF and OCR libraries that lead in quality.

A change aligns when it makes PDF text extraction more accurate, more honest about failures, easier to run with nothing configured, or safer to expose.
A change should be resisted when it makes a GPU, a vendor or a model mandatory, hides which method produced a page, stores files by default, or adds work that is not getting text out of PDFs.
