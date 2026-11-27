# Technical debt

### [2026-11-27] From: EST-9306-llm-pdf-ocr-api-record-the-est-9260-review-finding-in-the-technical-debt-list

| Group | | Severity | File | Problem | Fix | Category | Est Minutes | Source | Attempts |
|---|---|---|---|---|---|---|---|---|---|
| vision | [ ] | Low | src/llm_pdf_ocr/vision.py:230 | On every HTTP 429 the up-to-64 KiB body is read into memory, even when `config.demo` is False and `_budget_exceeded` is never consulted, so a non-demo rate-limited read now also waits for the gateway body before answering. | Read the body only when `config.demo` (`if status == 429 and config.demo:`), keeping the `Retry-After` header read as it is for all 429s. | performance | 15 | paperclip-specialist (finding by the Reviewer on EST-9260) | 0 |
