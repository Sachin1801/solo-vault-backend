"""
Lambda handler: fn-store

Multi-action handler invoked by Step Functions:
  - "store":       read embeddings from S3 and store vault.chunks/FTS
  - "mark_failed": mark vault.entries failed without deleting the row
  - "clone":       copy chunks from a same-user indexed entry with the same hash

Input:  Step Functions event with action field
Output: { status, entry_id }
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import boto3
import psycopg2
from pgvector.psycopg2 import register_vector

PIPELINE_BUCKET = os.environ.get("S3_BUCKET", "vault-local")
CHUNKER_VERSION = os.environ.get("CHUNKER_VERSION", "1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-m3")

_s3 = None
_secrets = None
_conn = None
_db_config = None


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


def _get_secrets():
    global _secrets
    if _secrets is None:
        _secrets = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    return _secrets


def _load_db_config() -> dict[str, Any]:
    global _db_config
    if _db_config is not None:
        return _db_config

    secret_arn = os.environ.get("DB_SECRET_ARN")
    if secret_arn:
        resp = _get_secrets().get_secret_value(SecretId=secret_arn)
        secret = json.loads(resp["SecretString"])
        _db_config = {
            "host": secret["host"],
            "port": int(secret.get("port", 5432)),
            "dbname": secret.get("dbname") or secret.get("database") or "vault",
            "user": secret.get("username") or secret.get("user"),
            "password": secret["password"],
        }
    else:
        _db_config = {
            "host": os.environ.get("DB_HOST", "localhost"),
            "port": int(os.environ.get("DB_PORT", "5432")),
            "dbname": os.environ.get("DB_NAME", "vault"),
            "user": os.environ.get("DB_USER", "vault"),
            "password": os.environ.get("DB_PASSWORD", "vault"),
        }
    return _db_config


def _get_conn():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(**_load_db_config(), connect_timeout=10)
        _conn.autocommit = False
        register_vector(_conn)
    return _conn


def _read_s3_json(key: str) -> Any:
    resp = _get_s3().get_object(Bucket=PIPELINE_BUCKET, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def _delete_s3_prefix(prefix: str) -> None:
    s3 = _get_s3()
    try:
        resp = s3.list_objects_v2(Bucket=PIPELINE_BUCKET, Prefix=prefix)
        for obj in resp.get("Contents", []):
            s3.delete_object(Bucket=PIPELINE_BUCKET, Key=obj["Key"])
    except Exception:
        pass


def _chunk_id(entry_id: str, chunk_index: int) -> str:
    return f"{entry_id}:{chunk_index}"


def _update_status(entry_id: str, status: str, user_id: str = "", error: str | None = None) -> None:
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            params: list[Any] = [status, status, status, error, status, entry_id]
            user_filter = ""
            if user_id:
                user_filter = " AND user_id = %s"
                params.append(user_id)
            cur.execute(
                f"""
                UPDATE vault.entries
                   SET index_status = %s,
                       cloud_sync_state = CASE
                           WHEN %s = 'indexed' THEN 'synced'
                           WHEN %s = 'failed' THEN 'failed'
                           ELSE cloud_sync_state
                       END,
                       index_error = %s,
                       indexed_at = CASE
                           WHEN %s = 'indexed' THEN EXTRACT(EPOCH FROM NOW())::bigint
                           ELSE indexed_at
                       END,
                       updated_at = EXTRACT(EPOCH FROM NOW())::bigint
                 WHERE id = %s{user_filter}
                """,
                params,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _store_embeddings(event: dict) -> dict:
    entry_id = event["entry_id"]
    user_id = event["user_id"]
    file_hash = event.get("file_hash", "")
    s3_key = event.get("s3_key", "")
    embeddings_key = event.get("embeddings_s3_key", f"pipeline/{entry_id}/embeddings.json")
    embeddings = _read_s3_json(embeddings_key)

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                  FROM vault.entries
                 WHERE id = %s
                   AND user_id = %s
                   AND (%s = '' OR vault_blob_path = %s)
                """,
                (entry_id, user_id, s3_key, s3_key),
            )
            if cur.fetchone() is None:
                raise ValueError("Entry ownership or S3 key mismatch")

            cur.execute("DELETE FROM vault.chunks_fts WHERE entry_id = %s", (entry_id,))
            cur.execute("DELETE FROM vault.chunks WHERE entry_id = %s", (entry_id,))

            for e in embeddings:
                chunk_index = int(e["chunk_index"])
                content = e["content"]
                chunk_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                chunk_id = _chunk_id(entry_id, chunk_index)
                cur.execute(
                    """
                    INSERT INTO vault.chunks
                      (id, entry_id, user_id, chunk_index, content, embedding, token_count, chunk_hash)
                    VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)
                    ON CONFLICT (entry_id, chunk_index) DO NOTHING
                    """,
                    (
                        chunk_id,
                        entry_id,
                        user_id,
                        chunk_index,
                        content,
                        e["embedding"],
                        e["token_count"],
                        chunk_hash,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO vault.chunks_fts (content, entry_id, chunk_id, user_id)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (content, entry_id, chunk_id, user_id),
                )

            cur.execute(
                """
                UPDATE vault.entries
                   SET index_status='indexed',
                       cloud_sync_state='synced',
                       chunk_count=%s,
                       embedding_model=%s,
                       chunker_version=%s,
                       file_hash=%s,
                       index_error=NULL,
                       indexed_at=EXTRACT(EPOCH FROM NOW())::bigint,
                       updated_at=EXTRACT(EPOCH FROM NOW())::bigint
                 WHERE id=%s AND user_id=%s
                """,
                (len(embeddings), EMBEDDING_MODEL, CHUNKER_VERSION, file_hash, entry_id, user_id),
            )

        conn.commit()
    except Exception as exc:
        conn.rollback()
        _update_status(entry_id, "failed", user_id, str(exc))
        raise

    _delete_s3_prefix(f"pipeline/{entry_id}/")
    return {"status": "indexed", "entry_id": entry_id}


def _clone_from_source(event: dict) -> dict:
    entry_id = event["entry_id"]
    user_id = event["user_id"]
    file_hash = event.get("file_hash", "")

    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, chunker_version, embedding_model, chunk_count
                  FROM vault.entries
                 WHERE file_hash = %s
                   AND user_id = %s
                   AND index_status = 'indexed'
                   AND id <> %s
                 LIMIT 1
                """,
                (file_hash, user_id, entry_id),
            )
            source = cur.fetchone()
            if source is None:
                conn.commit()
                return {"status": "failed", "entry_id": entry_id, "error": "No source entry to clone from"}

            src_entry_id, chunker_ver, emb_model, chunk_count = source
            cur.execute("DELETE FROM vault.chunks_fts WHERE entry_id = %s", (entry_id,))
            cur.execute("DELETE FROM vault.chunks WHERE entry_id = %s", (entry_id,))
            cur.execute(
                """
                INSERT INTO vault.chunks
                  (id, entry_id, user_id, chunk_index, content, embedding, token_count, chunk_hash)
                SELECT %s || ':' || chunk_index::text, %s, %s, chunk_index, content, embedding, token_count, chunk_hash
                  FROM vault.chunks
                 WHERE entry_id = %s
                ON CONFLICT (entry_id, chunk_index) DO NOTHING
                """,
                (entry_id, entry_id, user_id, src_entry_id),
            )
            cur.execute(
                """
                INSERT INTO vault.chunks_fts (content, entry_id, chunk_id, user_id)
                SELECT content, entry_id, id, user_id
                  FROM vault.chunks
                 WHERE entry_id = %s
                """,
                (entry_id,),
            )
            cur.execute(
                """
                UPDATE vault.entries
                   SET chunker_version=%s,
                       embedding_model=%s,
                       file_hash=%s,
                       chunk_count=%s,
                       index_status='indexed',
                       cloud_sync_state='synced',
                       index_error=NULL,
                       indexed_at=EXTRACT(EPOCH FROM NOW())::bigint,
                       updated_at=EXTRACT(EPOCH FROM NOW())::bigint
                 WHERE id=%s AND user_id=%s
                """,
                (chunker_ver, emb_model, file_hash, chunk_count, entry_id, user_id),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"status": "indexed", "entry_id": entry_id}


def handler(event: dict, context: Any) -> dict:
    action = event.get("action", "store")
    entry_id = event.get("entry_id", "unknown")
    user_id = event.get("user_id", "")

    if action == "store":
        return _store_embeddings(event)
    if action == "mark_indexed":
        _update_status(entry_id, "indexed", user_id)
        return {"status": "indexed", "entry_id": entry_id}
    if action == "mark_deleted":
        _update_status(entry_id, "failed", user_id, "S3 object missing")
        return {"status": "failed", "entry_id": entry_id}
    if action == "mark_failed":
        error = json.dumps(event.get("error", {})) if event.get("error") else None
        _update_status(entry_id, "failed", user_id, error)
        return {"status": "failed", "entry_id": entry_id}
    if action == "clone":
        return _clone_from_source(event)
    raise ValueError(f"Unknown action: {action}")
