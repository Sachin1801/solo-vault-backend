FROM python:3.12-slim

# EC2-like runtime defaults: predictable logging, smaller memory spikes, CPU-safe behavior.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TOKENIZERS_PARALLELISM=false \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    HF_HOME=/var/cache/huggingface \
    TRANSFORMERS_CACHE=/var/cache/huggingface

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    tesseract-ocr \
    curl \
    ca-certificates \
    tini \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt /app/
RUN pip install -r requirements.txt -r requirements-dev.txt

COPY . /app

# Run as non-root user similar to hardened EC2 service deployment.
RUN useradd -m -u 10001 appuser \
    && mkdir -p /var/cache/huggingface /app/.pytest_cache \
    && chown -R appuser:appuser /app /var/cache/huggingface

USER appuser
ENTRYPOINT ["/usr/bin/tini", "--"]
