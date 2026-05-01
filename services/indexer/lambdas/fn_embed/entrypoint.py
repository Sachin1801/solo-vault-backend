"""
ECS Fargate entrypoint: fn-embed

Reads chunks from S3, generates embeddings using BGE-M3, writes
embeddings back to S3.

This runs as a standalone ECS task invoked by Step Functions via
the ecs:RunTask.sync integration. It does NOT run on Lambda because
the BGE-M3 model is ~1.5 GB and requires ~4 GB RAM.

Environment variables (set by Step Functions container overrides):
  INPUT_S3_KEY   - S3 key to read chunks JSON from
  OUTPUT_S3_KEY  - S3 key to write embeddings JSON to
  ENTRY_ID       - Entry ID for logging
  S3_BUCKET      - Bucket name
  S3_ENDPOINT_URL - MinIO URL (local) or None (prod)
  S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION
  REDIS_URL      - ElastiCache URL for embedding cache
  EMBEDDING_MODEL - Model name (default: BAAI/bge-m3)
  EMBEDDING_DIM   - Vector dimension (default: 1024)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time

import boto3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BUCKET = os.environ.get("S3_BUCKET", "vault-local")
INPUT_KEY = os.environ.get("INPUT_S3_KEY", "")
OUTPUT_KEY = os.environ.get("OUTPUT_S3_KEY", "")
ENTRY_ID = os.environ.get("ENTRY_ID", "unknown")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))

# ---------------------------------------------------------------------------
# S3 client
# ---------------------------------------------------------------------------


def _get_s3():
    endpoint = os.environ.get("S3_ENDPOINT_URL") or None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY"),
        aws_secret_access_key=os.environ.get("S3_SECRET_KEY"),
        region_name=os.environ.get("S3_REGION", "us-east-1"),
    )


# ---------------------------------------------------------------------------
# Redis cache (optional -- gracefully degrades if unavailable)
# ---------------------------------------------------------------------------

_redis = None


def _get_redis():
    global _redis
    if _redis is not None:
        return _redis
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis
        _redis = redis.from_url(redis_url, decode_responses=True)
        _redis.ping()
        return _redis
    except Exception:
        return None


def _chunk_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _get_cached_embedding(chash: str) -> list[float] | None:
    r = _get_redis()
    if r is None:
        return None
    try:
        val = r.get(f"cache:chunk:{chash}")
        if val:
            return json.loads(val)
    except Exception:
        pass
    return None


def _cache_embedding(chash: str, vec: list[float]) -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        r.set(f"cache:chunk:{chash}", json.dumps(vec), ex=86400 * 30)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------


def _load_model():
    from FlagEmbedding import BGEM3FlagModel
    print(f"[fn-embed] Loading model {EMBEDDING_MODEL}...")
    t0 = time.time()
    model = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=False)
    print(f"[fn-embed] Model loaded in {time.time() - t0:.1f}s")
    return model


def _embed_batch(model, texts: list[str]) -> list[list[float]]:
    embeddings = model.encode(texts, return_dense=True)["dense_vecs"]
    vectors: list[list[float]] = []
    for emb in embeddings:
        vec = emb.tolist() if hasattr(emb, "tolist") else list(emb)
        if len(vec) > EMBEDDING_DIM:
            vec = vec[:EMBEDDING_DIM]
        elif len(vec) < EMBEDDING_DIM:
            vec = vec + [0.0] * (EMBEDDING_DIM - len(vec))
        vectors.append(vec)
    return vectors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    s3 = _get_s3()

    # 1. Read chunks from S3
    print(f"[fn-embed] Reading chunks from s3://{BUCKET}/{INPUT_KEY}")
    resp = s3.get_object(Bucket=BUCKET, Key=INPUT_KEY)
    chunks = json.loads(resp["Body"].read().decode("utf-8"))
    print(f"[fn-embed] Got {len(chunks)} chunks for entry {ENTRY_ID}")

    # 2. Load model
    model = _load_model()

    # 3. Embed with cache
    results = [None] * len(chunks)
    cache_misses: list[tuple[int, dict]] = []

    for i, chunk in enumerate(chunks):
        chash = _chunk_hash(chunk["content"])
        cached = _get_cached_embedding(chash)
        if cached is not None:
            vec = cached[:EMBEDDING_DIM]
            if len(vec) < EMBEDDING_DIM:
                vec = vec + [0.0] * (EMBEDDING_DIM - len(vec))
            results[i] = {
                "chunk_index": chunk["chunk_index"],
                "content": chunk["content"],
                "embedding": vec,
                "token_count": chunk["token_count"],
            }
        else:
            cache_misses.append((i, chunk))

    if cache_misses:
        print(f"[fn-embed] Embedding {len(cache_misses)} cache misses (batch)")
        texts = [c["content"] for _, c in cache_misses]

        # Batch in groups of 32
        BATCH_SIZE = 32
        all_vectors: list[list[float]] = []
        for batch_start in range(0, len(texts), BATCH_SIZE):
            batch = texts[batch_start : batch_start + BATCH_SIZE]
            all_vectors.extend(_embed_batch(model, batch))

        for (pos, chunk), vec in zip(cache_misses, all_vectors):
            chash = _chunk_hash(chunk["content"])
            _cache_embedding(chash, vec)
            results[pos] = {
                "chunk_index": chunk["chunk_index"],
                "content": chunk["content"],
                "embedding": vec,
                "token_count": chunk["token_count"],
            }

    embeddings = [r for r in results if r is not None]

    # 4. Write embeddings to S3
    output_key = OUTPUT_KEY or f"pipeline/{ENTRY_ID}/embeddings.json"
    payload = json.dumps(embeddings).encode("utf-8")
    print(f"[fn-embed] Writing {len(embeddings)} embeddings ({len(payload)} bytes) to s3://{BUCKET}/{output_key}")
    s3.put_object(Bucket=BUCKET, Key=output_key, Body=payload, ContentType="application/json")
    print(f"[fn-embed] Done for entry {ENTRY_ID}")


if __name__ == "__main__":
    main()
