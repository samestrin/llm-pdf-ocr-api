FROM python:3.12-slim
RUN apt-get update && apt-get clean \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY src/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
EXPOSE 5000
CMD ["gunicorn", "src.app:app", "-w", 4, "-b", "0.0.0.0:5000"]
