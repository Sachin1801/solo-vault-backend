# ECS Fargate container for BGE-M3 embedding
# Build: docker build -t fn-embed .
# Size: ~3 GB (model is ~1.5 GB)

FROM python:3.12-slim

WORKDIR /app

ENV HF_HOME=/opt/hf-cache \
    TRANSFORMERS_CACHE=/opt/hf-cache \
    HF_HUB_DISABLE_TELEMETRY=1 \
    PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps. Install CPU torch first so FlagEmbedding does not resolve a
# CUDA torch wheel from PyPI when its dependencies are installed.
RUN pip install --no-cache-dir \
    torch \
    --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir \
    awslambdaric \
    boto3 \
    redis \
    FlagEmbedding

# Pre-download model at build time. Runtime workers run offline so missing model
# cache fails during image build instead of hanging in a private subnet.
RUN python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3', use_fp16=False)"

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# Copy entrypoint
COPY entrypoint.py /app/entrypoint.py

ENTRYPOINT ["python", "/app/entrypoint.py"]
