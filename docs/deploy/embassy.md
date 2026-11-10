# Private review deploy on embassy

The owner's review copy of the service runs on embassy at `https://ocr.estr.in`, behind Traefik and an Authelia login. Every route, the web page and the API alike, answers only after that login. The deploy itself (Traefik route, Authelia rule, litellm key, first start) is fleet-deploy's: its `docs/review-stack.md` and `docs/apply/install-gateway.md` have the steps. This page covers what this repo ships for it: [`docker-compose.embassy.yml`](../../docker-compose.embassy.yml).

## What the compose file runs

- One service, `api`, built from this repo's `Dockerfile` (uvicorn on port 5000). No source is mounted into it.
- No host port. Traefik reaches it on the external Docker network `proxy`, where it also answers as `ocr`, so the chatbot backend on the same network can use `OCR_URL=http://ocr:5000`.
- No model server and no `ollama`. The vision method calls the fleet litellm at `orchestrator.lan:4000` with this app's own key.
- `restart: unless-stopped`. Health is the Dockerfile's healthcheck on `GET /health` (`{"status": "ok"}`); fleet-deploy's receiver waits for it and rolls back to the previous image if it fails.
- The project is named `llm-pdf-ocr-api`, so the image is `llm-pdf-ocr-api-api`. The receiver tags it `:rollback` before each deploy.

## Settings: `.env.embassy`

Every setting comes from `.env.embassy` beside the compose file on embassy (`~/docker/llm-pdf-ocr-api/.env.embassy`, mode 600). It is never in git (`.gitignore` lists it), and the receiver refuses a deploy that carries one. Compose will not start without it.

| Variable | What to set |
| --- | --- |
| `OCR_LLM_BASE_URL` | The fleet litellm's API root: `http://orchestrator.lan:4000/v1`. |
| `OCR_LLM_API_KEY` | This app's own litellm key (alias `review-llm-pdf-ocr-api`). It is never written anywhere else. |
| `OCR_LLM_DEFAULT_MODEL` | A model the key allows that accepts images. |
| `OCR_LLM_ALLOWED_MODELS` | Comma list of other models the key allows that callers may pick; leave empty for the default only. |

Any other setting in the [Configuration](../../README.md#configuration) table may be added, for example `OCR_LLM_FALLBACK_TO_TESSERACT=true` so a page the model fails is read with Tesseract.

## Check it

Validate the file anywhere (CI does this with a placeholder settings file):

```bash
touch .env.embassy
docker compose -f docker-compose.embassy.yml config -q
```

Through Traefik, without a login, `https://ocr.estr.in/health` answers with a redirect to Authelia or 401, never with the app. With the owner's login it answers `{"status": "ok"}`.
