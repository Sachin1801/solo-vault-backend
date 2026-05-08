from pgvector.psycopg2 import register_vector

from app.cache.hashing import chunk_hash
from app.cache.redis_cache import mark_file_indexed
from app.config import CHUNKER_VERSION, settings
from app.db.connection import transaction
from app.types import EmbedResult, PipelineError, PipelineJob


def store(job: PipelineJob, embeddings: list[EmbedResult]) -> None:
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id
            FROM vault.entries
            WHERE id = %s AND user_id = %s AND vault_blob_path = %s
            """,
            (job.entry_id, job.user_id, job.s3_key),
        )
        if cur.fetchone() is None:
            raise PipelineError("Entry ownership or S3 key mismatch", "ENTRY_AUTH_MISMATCH")

        cur.execute("DELETE FROM vault.chunks_fts WHERE entry_id = %s", (job.entry_id,))
        cur.execute("DELETE FROM vault.chunks WHERE entry_id = %s", (job.entry_id,))
        register_vector(conn)
        rows = [
            (
                f"{job.entry_id}:{e.chunk_index}",
                job.entry_id,
                job.user_id,
                e.chunk_index,
                e.content,
                e.embedding,
                e.token_count,
                chunk_hash(e.content),
            )
            for e in embeddings
        ]
        cur.executemany(
            """
            INSERT INTO vault.chunks
              (id, entry_id, user_id, chunk_index, content, embedding, token_count, chunk_hash)
            VALUES (%s, %s, %s, %s, %s, %s::vector, %s, %s)
            ON CONFLICT (entry_id, chunk_index) DO NOTHING
            """,
            rows,
        )
        cur.executemany(
            """
            INSERT INTO vault.chunks_fts (content, entry_id, chunk_id, user_id)
            VALUES (%s, %s, %s, %s)
            """,
            [
                (
                    e.content,
                    job.entry_id,
                    f"{job.entry_id}:{e.chunk_index}",
                    job.user_id,
                )
                for e in embeddings
            ],
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
            (
                len(embeddings),
                settings.embedding_model,
                CHUNKER_VERSION,
                job.file_hash,
                job.entry_id,
                job.user_id,
            ),
        )

    if job.file_hash:
        mark_file_indexed(job.file_hash, job.user_id, job.entry_id)
