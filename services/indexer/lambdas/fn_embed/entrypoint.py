"""
ECS Fargate entrypoint: fn-embed

Runs in two modes:
  - worker mode: long-running ECS service polls SQS, keeps BGE-M3 loaded, and
    invokes fn-store after writing embeddings.
  - one-shot mode: legacy compatibility path for INPUT_S3_KEY/OUTPUT_S3_KEY.

The worker mode is the production path because it avoids per-file Fargate image
pulls and model-load cold starts.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from typing import Any

import boto3

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BUCKET = os.environ.get("S3_BUCKET", "vault-local")
INPUT_KEY = os.environ.get("INPUT_S3_KEY", "")
OUTPUT_KEY = os.environ.get("OUTPUT_S3_KEY", "")
ENTRY_ID = os.environ.get("ENTRY_ID", "unknown")
EMBED_QUEUE_URL = os.environ.get("EMBED_QUEUE_URL", "")
STORE_FUNCTION_NAME = os.environ.get("STORE_FUNCTION_NAME", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))
EMBED_JOB_VISIBILITY_TIMEOUT = int(os.environ.get("EMBED_JOB_VISIBILITY_TIMEOUT", "1800"))
EMBED_JOB_MAX_ATTEMPTS = int(os.environ.get("EMBED_JOB_MAX_ATTEMPTS", "3"))

_running = True
_s3 = None
_sqs = None
_lambda = None
_redis = None
_model = None


def _log(message: str, **fields: Any) -> None:
    payload = {"event": "vault.pipeline.embed", "message": message, **fields}
    print(json.dumps(payload, default=str), flush=True)


def _handle_signal(signum: int, _frame: Any) -> None:
    global _running
    _running = False
    _log("shutdown_requested", signal=signum)


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ---------------------------------------------------------------------------
# AWS clients
# ---------------------------------------------------------------------------


def _get_s3():
    global _s3
    if _s3 is None:
        endpoint = os.environ.get("S3_ENDPOINT_URL") or None
        _s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.environ.get("S3_ACCESS_KEY"),
            aws_secret_access_key=os.environ.get("S3_SECRET_KEY"),
            region_name=os.environ.get("S3_REGION", "us-east-1"),
        )
    return _s3


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=os.environ.get("S3_REGION", "us-east-1"))
    return _sqs


def _get_lambda():
    global _lambda
    if _lambda is None:
        _lambda = boto3.client("lambda", region_name=os.environ.get("S3_REGION", "us-east-1"))
    return _lambda


# ---------------------------------------------------------------------------
# Redis cache (optional -- gracefully degrades if unavailable)
# ---------------------------------------------------------------------------


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
    except Exception as exc:
        _log("redis_unavailable", error=str(exc))
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


def _query_hash(query: str) -> str:
    normalized = " ".join(query.strip().split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _get_cached_query_embedding(qhash: str) -> list[float] | None:
    r = _get_redis()
    if r is None:
        return None
    try:
        val = r.get(f"cache:query:{EMBEDDING_MODEL}:{EMBEDDING_DIM}:{qhash}")
        if val:
            return json.loads(val)
    except Exception as exc:
        _log("query_cache_read_failed", error=str(exc))
    return None


def _cache_query_embedding(qhash: str, vec: list[float]) -> None:
    r = _get_redis()
    if r is None:
        return
    try:
        r.set(f"cache:query:{EMBEDDING_MODEL}:{EMBEDDING_DIM}:{qhash}", json.dumps(vec), ex=86400 * 7)
    except Exception as exc:
        _log("query_cache_write_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------


def _load_model():
    global _model
    if _model is not None:
        return _model

    from FlagEmbedding import BGEM3FlagModel

    _log(
        "model_load_started",
        model=EMBEDDING_MODEL,
        hf_home=os.environ.get("HF_HOME", ""),
        transformers_cache=os.environ.get("TRANSFORMERS_CACHE", ""),
        hf_hub_offline=os.environ.get("HF_HUB_OFFLINE", ""),
        transformers_offline=os.environ.get("TRANSFORMERS_OFFLINE", ""),
    )
    t0 = time.time()
    model = BGEM3FlagModel(EMBEDDING_MODEL, use_fp16=False)
    _log("model_loaded", model=EMBEDDING_MODEL, elapsed_seconds=round(time.time() - t0, 2))
    _model = model
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


def _embed_chunks(model, chunks: list[dict[str, Any]], entry_id: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any] | None] = [None] * len(chunks)
    cache_misses: list[tuple[int, dict[str, Any]]] = []

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

    _log(
        "cache_checked",
        entry_id=entry_id,
        chunk_count=len(chunks),
        cache_hits=len(chunks) - len(cache_misses),
        cache_misses=len(cache_misses),
    )

    if cache_misses:
        _log("embedding_batch_started", entry_id=entry_id, count=len(cache_misses))
        texts = [c["content"] for _, c in cache_misses]
        batch_size = 32
        all_vectors: list[list[float]] = []
        for batch_start in range(0, len(texts), batch_size):
            batch = texts[batch_start : batch_start + batch_size]
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
        _log("embedding_batch_completed", entry_id=entry_id, count=len(cache_misses))

    return [r for r in results if r is not None]


def _embed_query_text(query: str) -> list[float]:
    q = " ".join(query.strip().split())
    if not q:
        raise ValueError("query must not be empty")

    qhash = _query_hash(q)
    cached = _get_cached_query_embedding(qhash)
    if cached is not None:
        vec = cached[:EMBEDDING_DIM]
        if len(vec) < EMBEDDING_DIM:
            vec = vec + [0.0] * (EMBEDDING_DIM - len(vec))
        _log("query_cache_hit", model=EMBEDDING_MODEL, dim=EMBEDDING_DIM)
        return vec

    model = _load_model()
    vec = _embed_batch(model, [q])[0]
    _cache_query_embedding(qhash, vec)
    _log("query_embedded", model=EMBEDDING_MODEL, dim=len(vec), cached=False)
    return vec


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Synchronous query-embedding Lambda entrypoint.

    Request shape: {"query": "..."} or {"action": "embed_query", "query": "..."}.
    This intentionally returns the same model/dim contract used by chunk
    indexing so vault search never compares incompatible vector spaces.
    """
    if not isinstance(event, dict):
        raise ValueError("event must be a JSON object")
    action = event.get("action", "embed_query")
    if action != "embed_query":
        raise ValueError(f"unsupported action: {action}")
    query_text = event.get("query")
    if not isinstance(query_text, str):
        raise ValueError("query must be a string")

    vec = _embed_query_text(query_text)
    return {
        "embedding": vec,
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
    }


# ---------------------------------------------------------------------------
# Job processing
# ---------------------------------------------------------------------------


def _read_chunks(bucket: str, input_key: str) -> list[dict[str, Any]]:
    _log("chunks_read_started", bucket=bucket, input_s3_key=input_key)
    resp = _get_s3().get_object(Bucket=bucket, Key=input_key)
    chunks = json.loads(resp["Body"].read().decode("utf-8"))
    _log("chunks_read_completed", bucket=bucket, input_s3_key=input_key, chunk_count=len(chunks))
    return chunks


def _write_embeddings(bucket: str, output_key: str, embeddings: list[dict[str, Any]], entry_id: str) -> None:
    payload = json.dumps(embeddings).encode("utf-8")
    _log(
        "embeddings_write_started",
        entry_id=entry_id,
        bucket=bucket,
        output_s3_key=output_key,
        embedding_count=len(embeddings),
        payload_bytes=len(payload),
    )
    _get_s3().put_object(Bucket=bucket, Key=output_key, Body=payload, ContentType="application/json")
    _log("embeddings_write_completed", entry_id=entry_id, bucket=bucket, output_s3_key=output_key)


def _invoke_store(payload: dict[str, Any]) -> dict[str, Any]:
    if not STORE_FUNCTION_NAME:
        raise RuntimeError("STORE_FUNCTION_NAME is not configured")
    _log(
        "store_invoke_started",
        entry_id=payload.get("entry_id", ""),
        user_id=payload.get("user_id", ""),
        action=payload.get("action", "store"),
        function=STORE_FUNCTION_NAME,
    )
    resp = _get_lambda().invoke(
        FunctionName=STORE_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    body = resp["Payload"].read().decode("utf-8")
    if resp.get("FunctionError"):
        raise RuntimeError(f"store lambda failed: {body}")
    try:
        result = json.loads(body) if body else {}
    except json.JSONDecodeError:
        result = {"raw": body}
    _log(
        "store_invoke_completed",
        entry_id=payload.get("entry_id", ""),
        user_id=payload.get("user_id", ""),
        action=payload.get("action", "store"),
        result=result,
    )
    return result


def _mark_failed(job: dict[str, Any], error: Exception) -> None:
    if not STORE_FUNCTION_NAME:
        _log("mark_failed_skipped", reason="STORE_FUNCTION_NAME not configured", error=str(error))
        return
    _invoke_store(
        {
            "action": "mark_failed",
            "entry_id": job.get("entry_id", "unknown"),
            "user_id": job.get("user_id", ""),
            "error": {
                "Error": error.__class__.__name__,
                "Cause": str(error),
            },
        }
    )


def _process_job(model, job: dict[str, Any]) -> None:
    entry_id = job["entry_id"]
    user_id = job["user_id"]
    bucket = job.get("bucket") or BUCKET
    input_key = job.get("chunks_s3_key") or job.get("input_s3_key") or INPUT_KEY
    output_key = job.get("embeddings_s3_key") or job.get("output_s3_key") or f"pipeline/{entry_id}/embeddings.json"
    if not input_key:
        raise ValueError("Missing chunks_s3_key/input_s3_key")

    _log(
        "job_started",
        entry_id=entry_id,
        user_id=user_id,
        bucket=bucket,
        input_s3_key=input_key,
        output_s3_key=output_key,
    )
    chunks = _read_chunks(bucket, input_key)
    embeddings = _embed_chunks(model, chunks, entry_id)
    _write_embeddings(bucket, output_key, embeddings, entry_id)
    _invoke_store(
        {
            "action": "store",
            "entry_id": entry_id,
            "user_id": user_id,
            "file_hash": job.get("file_hash", ""),
            "bucket": bucket,
            "s3_key": job.get("s3_key", ""),
            "embeddings_s3_key": output_key,
        }
    )
    _log("job_completed", entry_id=entry_id, user_id=user_id, embedding_count=len(embeddings))


def _one_shot_job_from_env() -> dict[str, Any]:
    return {
        "entry_id": ENTRY_ID,
        "user_id": os.environ.get("USER_ID", ""),
        "bucket": BUCKET,
        "chunks_s3_key": INPUT_KEY,
        "embeddings_s3_key": OUTPUT_KEY or f"pipeline/{ENTRY_ID}/embeddings.json",
        "file_hash": os.environ.get("FILE_HASH", ""),
        "s3_key": os.environ.get("SOURCE_S3_KEY", ""),
    }


def _run_one_shot() -> None:
    model = _load_model()
    _process_job(model, _one_shot_job_from_env())


def _run_worker() -> None:
    if not EMBED_QUEUE_URL:
        raise RuntimeError("EMBED_QUEUE_URL is required in worker mode")
    model = _load_model()
    _log(
        "worker_ready",
        queue_url=EMBED_QUEUE_URL,
        store_function=STORE_FUNCTION_NAME,
        visibility_timeout=EMBED_JOB_VISIBILITY_TIMEOUT,
        max_attempts=EMBED_JOB_MAX_ATTEMPTS,
    )
    sqs = _get_sqs()
    last_idle_log = 0.0

    while _running:
        resp = sqs.receive_message(
            QueueUrl=EMBED_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=EMBED_JOB_VISIBILITY_TIMEOUT,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = resp.get("Messages", [])
        if not messages and time.time() - last_idle_log >= 60:
            _log("worker_idle", queue_url=EMBED_QUEUE_URL)
            last_idle_log = time.time()
        for msg in messages:
            receipt_handle = msg["ReceiptHandle"]
            receive_count = int(msg.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
            job: dict[str, Any] = {}
            try:
                job = json.loads(msg["Body"])
                _log(
                    "message_received",
                    message_id=msg.get("MessageId", ""),
                    entry_id=job.get("entry_id", "unknown"),
                    receive_count=receive_count,
                )
                _process_job(model, job)
                sqs.delete_message(QueueUrl=EMBED_QUEUE_URL, ReceiptHandle=receipt_handle)
                _log("message_deleted", entry_id=job.get("entry_id", "unknown"), receive_count=receive_count)
            except Exception as exc:
                _log(
                    "job_failed",
                    entry_id=job.get("entry_id", "unknown"),
                    receive_count=receive_count,
                    error=str(exc),
                )
                if receive_count >= EMBED_JOB_MAX_ATTEMPTS:
                    try:
                        _mark_failed(job, exc)
                    finally:
                        sqs.delete_message(QueueUrl=EMBED_QUEUE_URL, ReceiptHandle=receipt_handle)
                        _log("failed_message_deleted", entry_id=job.get("entry_id", "unknown"))

    _log("worker_stopped")


def main() -> None:
    if EMBED_QUEUE_URL:
        _run_worker()
    else:
        _run_one_shot()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _log("fatal", error=str(exc))
        raise
