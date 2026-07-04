FROM python:3.11-slim

# Install system dependencies (libsndfile1 is required by the soundfile library)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy packaging and install base dependencies (optimizing for CPU-only to avoid massive CUDA downloads)
COPY pyproject.toml .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

# Copy application source code and web static assets
COPY app/ app/
COPY web/ web/

# Install the transcriptor package itself (non-editable install for production containers)
RUN pip install --no-cache-dir .

# Expose the API port
EXPOSE 8000

# Default entrypoint starts the API gateway
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
