"""
Lambda handler: fn-store

Multi-action handler invoked by Step Functions:
  - "store":        Read embeddings from S3, upsert into pgvector, cleanup
  - "mark_indexed": Update vault_entries.index_status = 'indexed'
  - "mark_deleted": Update vault_entries.index_status = 'deleted'
  - "mark_failed":  Update vault_entries.index_status = 'failed'
  - "clone":        Copy chunks from a source entry with the same file_hash

Input:  Step Functions event with action field
Output: { status, entry_id }

VPC: Yes (needs RDS access)
"""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
import psycopg2
from pgvector.psycopg2 import register_vector

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

PIPELINE_BUCKET = os.environ.get("S3_BUCKET", "vault-local")
CHUNKER_VERSION = os.environ.get("CHUNKER_VERSION", "1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")

# ---------------------------------------------------------------------------
# S3 client (lazy singleton)
# ---------------------------------------------------------------------------

_s3 = None


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


def _read_s3_json(key: str) -> Any:
    resp = _get_s3().get_object(Bucket=PIPELINE_BUCKET, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def _delete_s3_prefix(prefix: str) -> None:
    """Delete all objects under a prefix (pipeline cleanup)."""
    s3 = _get_s3()
    try:
        resp = s3.list_objects_v2(Bucket=PIPELINE_BUCKET, Prefix=prefix)
        for obj in resp.get("Contents", []):
            s3.delete_object(Bucket=PIPELINE_BUCKET, Key=obj["Key"])
    except Exception:
        pass  # Best-effort cleanup


# ---------------------------------------------------------------------------
# DB connection (single connection per Lambda invocation -- not a pool)
# Use RDS Proxy in production for connection pooling.
# ---------------------------------------------------------------------------

_conn = None


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", "5432")),
            dbname=os.environ.get("DB_NAME", "vault"),
            user=os.environ.get("DB_USER", "vault"),
            password=os.environ.get("DB_PASSWORD", "vault"),
            connect_timeout=10,
        )
        _conn.autocommit = False
        register_vector(_conn)
    return _conn


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _update_status(entry_id: str, status: str) -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE vault_entries SET index_status = %s, updated_at = NOW() WHERE entry_id = %s",
                (status, entry_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _store_embeddings(event: dict) -> dict:
    entry_id = event["entry_id"]
    user_id = event.get("user_id", "")
    file_hash = event.get("file_hash", "")

    # Read embeddings from S3
    embeddings_key = event.get("embeddings_s3_key", f"pipeline/{entry_id}/embeddings.json")
    embeddings = _read_s3_json(embeddings_key)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            # Idempotent: delete old chunks first
            cur.execute("DELETE FROM vault_chunks WHERE entry_id = %s", (entry_id,))

            # Batch insert
            import hashlib

            for e in embeddings:
                chunk_hash = hashlib.sha256(e["content"].encode("utf-8")).hexdigest()
                cur.execute(
                    """
                    INSERT INTO vault_chunks
                      (entry_id, user_id, chunk_index, content, embedding, token_count, chunk_hash)
                    VALUES (%s, %s, %s, %s, %s::vector, %s, %s)
                    ON CONFLICT (entry_id, chunk_index) DO NOTHING
                    """,
                    (
                        entry_id,
                        user_id,
                        e["chunk_index"],
                        e["content"],
                        e["embedding"],
                        e["token_count"],
                        chunk_hash,
                    ),
                )

            # Update entry metadata
            cur.execute(
                """
                UPDATE vault_entries
                SET index_status='indexed',
                    chunk_count=%s,
                    embedding_model=%s,
                    chunker_version=%s,
                    file_hash=%s,
                    updated_at=NOW()
                WHERE entry_id=%s
                """,
                (len(embeddings), EMBEDDING_MODEL, CHUNKER_VERSION, file_hash, entry_id),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # Cleanup intermediate pipeline files
    _delete_s3_prefix(f"pipeline/{entry_id}/")

    return {"status": "indexed", "entry_id": entry_id}


def _clone_from_source(event: dict) -> dict:
    entry_id = event["entry_id"]
    user_id = event.get("user_id", "")
    file_hash = event.get("file_hash", "")

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT entry_id, chunker_version, embedding_model, chunk_count
                FROM vault_entries
                WHERE file_hash = %s AND index_status = 'indexed' AND entry_id <> %s
                LIMIT 1
                """,
                (file_hash, entry_id),
            )
            source = cur.fetchone()
            if source is None:
                conn.commit()
                return {"status": "failed", "entry_id": entry_id, "error": "No source entry to clone from"}

            src_entry_id, chunker_ver, emb_model, chunk_count = source

            cur.execute("DELETE FROM vault_chunks WHERE entry_id = %s", (entry_id,))
            cur.execute(
                """
                INSERT INTO vault_chunks
                  (entry_id, user_id, chunk_index, content, embedding, token_count, chunk_hash)
                SELECT %s, %s, chunk_index, content, embedding, token_count, chunk_hash
                FROM vault_chunks WHERE entry_id = %s
                ON CONFLICT (entry_id, chunk_index) DO NOTHING
                """,
                (entry_id, user_id, src_entry_id),
            )
            cur.execute(
                """
                UPDATE vault_entries
                SET chunker_version=%s, embedding_model=%s, file_hash=%s,
                    chunk_count=%s, index_status='indexed', updated_at=NOW()
                WHERE entry_id=%s
                """,
                (chunker_ver, emb_model, file_hash, chunk_count, entry_id),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"status": "indexed", "entry_id": entry_id}


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    action = event.get("action", "store")
    entry_id = event.get("entry_id", "unknown")

    if action == "store":
        return _store_embeddings(event)
    elif action == "mark_indexed":
        _update_status(entry_id, "indexed")
        return {"status": "indexed", "entry_id": entry_id}
    elif action == "mark_deleted":
        _update_status(entry_id, "deleted")
        return {"status": "deleted", "entry_id": entry_id}
    elif action == "mark_failed":
        _update_status(entry_id, "failed")
        return {"status": "failed", "entry_id": entry_id}
    elif action == "clone":
        return _clone_from_source(event)
    else:
        raise ValueError(f"Unknown action: {action}")
