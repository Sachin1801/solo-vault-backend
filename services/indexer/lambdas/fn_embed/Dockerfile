# ECS Fargate container for BGE-M3 embedding
# Build: docker build -t fn-embed .
# Size: ~3 GB (model is ~1.5 GB)

FROM python:3.12-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Python deps
RUN pip install --no-cache-dir \
    boto3 \
    redis \
    FlagEmbedding \
    torch --index-url https://download.pytorch.org/whl/cpu

# Pre-download model at build time (cached in Docker layer)
RUN python -c "from FlagEmbedding import BGEM3FlagModel; BGEM3FlagModel('BAAI/bge-m3', use_fp16=False)" \
    || echo "Model download may complete at runtime"

# Copy entrypoint
COPY entrypoint.py /app/entrypoint.py

ENTRYPOINT ["python", "/app/entrypoint.py"]
