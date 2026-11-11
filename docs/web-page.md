# The web page

**Endpoint:** `/` **Method:** GET

| Light | Dark |
| --- | --- |
| ![The web page in the light theme at desktop width, after reading a two-page PDF: each page's image beside its text, then the full Markdown with copy and download buttons](screenshot-web-page-light-desktop.png) | ![The web page in the dark theme at desktop width, after reading a two-page PDF: each page's image beside its text, then the full Markdown with copy and download buttons](screenshot-web-page-dark-desktop.png) |
| ![The web page in the light theme at phone width, with the upload card, the two pages stacked and the Markdown output](screenshot-web-page-light-phone.png) | ![The web page in the dark theme at phone width, with the upload card, the two pages stacked and the Markdown output](screenshot-web-page-dark-phone.png) |

A page to use the service from a browser: drop or pick a PDF, choose Markdown or JSON and a method, and follow the job's progress. The result shows each page's image beside its text, the method that read it, and why it failed if it did; the full Markdown or JSON follows. The Markdown can be copied to the clipboard (which the browser allows on `localhost` or HTTPS), and the result downloaded as `result.md` or `result.json`. The page fits a phone-width screen, where each page's image sits above its text. Page text is shown as text, never as HTML, and a job that has expired is reported as an error.

## What is on the page

- The header names the app, LLM PDF OCR, with its mark, and has the theme button.
- The form states this service's real limits, filled in from `OCR_MAX_FILE_MB` and `OCR_MAX_PAGES` when the page is served (for example "One PDF, up to 50 MB and 500 pages."), and says what happens to your file: it is dropped when its job ends, and the text and page images are deleted `OCR_JOB_TTL_SECONDS` later (shown in words, such as "1 hour").
- **Try a sample** loads a small bundled PDF (`/static/samples/sample.pdf`, a copy of `tests/fixtures/mixed.pdf`, 15 KB) and reads it, so you can see a result without a file of your own. Its four pages show both routes: pages read from the text layer and scanned pages read by Tesseract.
- Before a file is chosen, the results panel says what you will see and explains each method in one line: Auto, Text layer, Tesseract and, when the operator configured one, Vision model.
- While a job runs, the status line says "Reading page 2 of 3" with three moving dots after it (just "Reading" until the job knows its pages), above a progress bar. A failure is shown as a red banner with the API's message and its request ID.

## Theme

The page follows the browser's light or dark theme. The theme button in the header overrides it; its label names what a click does ("Dark theme" or "Light theme"), and the choice is saved in the browser (`localStorage`, key `theme`). With no saved choice, the page follows the system again. People who ask for reduced motion see no animation.

## Look

The page shares one design system with the owner's other demo apps, taken from llm-pdf-chatbot: the same type, spacing, radii, surfaces, focus ring and controls, with this app's own violet accent and page-and-scan mark. Every text colour passes WCAG AA (4.5:1) in both themes; `tests/test_browser.py` measures them in a real browser. The accent pairs:

| Pair | Light | Dark |
| --- | --- | --- |
| Accent on the surface | `#6D28D9` on `#FFFFFF`: 7.10 | `#C4B5FD` on `#171A20`: 9.44 |
| Accent on the page background | `#6D28D9` on `#F4F5F7`: 6.51 | `#C4B5FD` on `#0F1115`: 10.24 |
| Accent on its subtle fill | `#6D28D9` on `#F1ECFD`: 6.14 | `#C4B5FD` on `#2A2140`: 8.18 |
| Accent hover on the surface | `#5B21B6` on `#FFFFFF`: 8.98 | `#DDD6FE` on `#2A2140`: 10.88 |
| White on the primary button | `#FFFFFF` on `#6D28D9`: 7.10 | `#FFFFFF` on `#7C3AED`: 5.70 |

## How it is built

It is plain HTML, CSS and JavaScript shipped inside the package (`src/llm_pdf_ocr/web/`, no build step), served with `Content-Security-Policy: default-src 'self'`, and loads nothing from any other origin, so it works with no network. For an API-only deployment, set `OCR_WEB_PAGE=false`: `/` and the page's files then answer 404, and every API endpoint works as before.

## Screenshots

The screenshots here and in the README are made by one command, from the repository root:

```bash
uv run python docs/make_screenshots.py
```

It serves the real page and stubs the jobs API in the browser with a fixed two-page result, so the images are the same on every machine and need neither Tesseract nor a model. It writes the four images above and `screenshot-web-page-empty-light-desktop.png`, the page before a file is chosen.
