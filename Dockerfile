# First stage: build and install dependencies
FROM python:3.12-slim as builder
WORKDIR /app
ENV PATH="/app/venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv venv
COPY src/*.txt ./
RUN pip install --no-cache-dir --timeout=120 -r requirements.txt

# Second stage: create the final image
FROM python:3.12-slim
WORKDIR /app
COPY --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"
COPY src/ ./
EXPOSE 5000
CMD ["gunicorn", "src.app:app", "-w", "4", "-b", "0.0.0.0:5000"]
