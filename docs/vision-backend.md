# Vision model backend

A vision model is optional. With none configured, every page is read from its text layer or with Tesseract, exactly as before. With one configured, `method=auto` sends each page that has no usable text layer to the model, which returns it as Markdown with tables, and the page names the model that read it.

The model is any endpoint that speaks the OpenAI-compatible `chat/completions` API and accepts images. It can run on your own machine or at a cloud provider; the service ships no model, downloads none and has no default one. You pick the endpoint and the model.

## Settings

All settings are environment variables, read once when the service starts. A bad value stops the service at start with a message naming the variable.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OCR_LLM_BASE_URL` | unset (off) | The endpoint's API root, ending in `/v1`. The service posts to `{OCR_LLM_BASE_URL}/chat/completions`. Leave it unset to turn the backend off. |
| `OCR_LLM_DEFAULT_MODEL` | none | The model used when a request names none. Required when `OCR_LLM_BASE_URL` is set. |
| `OCR_LLM_ALLOWED_MODELS` | the default model only | Comma list of other models a caller may pick with the `model` field. Any other name gets 400 `model_not_allowed`. |
| `OCR_LLM_API_KEY` | none | Sent as `Authorization: Bearer <key>`. Leave unset for an endpoint that needs no key. |
| `OCR_LLM_TIMEOUT_SECONDS` | `60` | How long to wait for one page. |
| `OCR_LLM_CONCURRENCY` | `2` | Most pages in flight at once, across all requests. |
| `OCR_LLM_FALLBACK_TO_TESSERACT` | `false` | Whether a page the model cannot read may be read with Tesseract instead. Read and checked at start; for now a page the model cannot read is always returned as `failed` with `vision_failed`. |

Each page is rendered at 150 DPI and sent as a PNG with a fixed prompt. Callers cannot change the prompt.

The API key and the base URL never appear in a response, an error or a log line, and the service refuses redirects, so the key is only ever sent to the configured endpoint.

## A local model

A local server keeps page images on your machine. Both servers below need a model that accepts images; check its model card.

### Local: vLLM

Serve the model under a short name, so the service's settings do not change when you swap the model:

```bash
vllm serve <vision model> --served-model-name page-reader --port 8000
```

```dotenv
OCR_LLM_BASE_URL=http://localhost:8000/v1
OCR_LLM_DEFAULT_MODEL=page-reader
# Only if you started vLLM with --api-key:
# OCR_LLM_API_KEY=change-me
OCR_LLM_CONCURRENCY=4
```

vLLM batches requests, so a higher `OCR_LLM_CONCURRENCY` usually helps; lower it if pages time out.

### Local: Ollama

Pull a model that accepts images and copy it to a short name:

```bash
ollama pull <vision model>
ollama cp <vision model> page-reader
```

```dotenv
OCR_LLM_BASE_URL=http://localhost:11434/v1
OCR_LLM_DEFAULT_MODEL=page-reader
OCR_LLM_TIMEOUT_SECONDS=120
OCR_LLM_CONCURRENCY=1
```

Ollama needs no API key. On a CPU, or with one model loaded at a time, keep `OCR_LLM_CONCURRENCY` at 1 and allow a longer timeout.

### When the service runs in Docker

Inside the container, `localhost` is the container itself. Point the base URL at the host instead:

```bash
docker run --add-host=host.docker.internal:host-gateway \
  -e OCR_LLM_BASE_URL=http://host.docker.internal:11434/v1 \
  -e OCR_LLM_DEFAULT_MODEL=page-reader \
  -p 5000:5000 llm-pdf-ocr-api
```

The model server must listen on an address the container can reach, not only on `127.0.0.1`.

## A cloud model

Any provider with an OpenAI-compatible endpoint works. Take the API root, the key and the model names from your provider's documentation:

### Cloud: any OpenAI-compatible provider

```dotenv
OCR_LLM_BASE_URL=https://llm.example.com/v1
OCR_LLM_API_KEY=your-provider-key
OCR_LLM_DEFAULT_MODEL=provider-vision-model
OCR_LLM_ALLOWED_MODELS=provider-vision-model-large
OCR_LLM_TIMEOUT_SECONDS=60
OCR_LLM_CONCURRENCY=2
```

With a cloud endpoint, the image of every page the model reads leaves your machine. Set `OCR_LLM_CONCURRENCY` within your provider's rate limit: a page that gets HTTP 429 is returned as `failed`.

Keep the key out of the image and out of version control: pass it with `docker run --env-file` or your orchestrator's secrets.

## Checking it works

Send a scanned page with `method=vision` and look at the page's `method` and `model`:

```bash
curl -F file=@scanned.pdf -F format=json -F method=vision localhost:5000/v1/extract
```

A page the model could not read is `failed` with `vision_failed` and a message saying why, such as a timeout or HTTP 429; the other pages are still returned. `method=vision` with no backend configured gets 400 `vision_not_configured`.
