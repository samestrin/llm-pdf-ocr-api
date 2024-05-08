# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory to /app
WORKDIR /app

# Install system dependencies required for your project
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Set up a virtual environment within the container
RUN python -m venv venv

# Activate the virtual environment
ENV PATH="/app/venv/bin:$PATH"

# Copy the requirements files
COPY src/core.txt src/image_processing.txt src/ml.txt src/nlp.txt ./

# Install Python dependencies inside the virtual environment
# Install core web functionality
RUN pip install --no-cache-dir --timeout=120 -r core.txt

# Install image processing libraries
RUN pip install --no-cache-dir --timeout=120 -r image_processing.txt

# Install ML libraries
RUN pip install --no-cache-dir --timeout=120 -r ml.txt

# Install NLP libraries
RUN pip install --no-cache-dir --timeout=120 -r nlp.txt


# Copy the rest of the application into the container
COPY . .

# Make port 5000 available to the world outside this container
EXPOSE 5000

# Run the application
CMD ["gunicorn", "src.app:app", "-w", "4", "-b", "0.0.0.0:5000"]
