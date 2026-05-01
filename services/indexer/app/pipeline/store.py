from pgvector.psycopg2 import register_vector

from app.cache.hashing import chunk_hash
from app.cache.redis_cache import mark_file_indexed
from app.config import CHUNKER_VERSION, settings
from app.db.connection import transaction
from app.types import EmbedResult, PipelineJob


def store(job: PipelineJob, embeddings: list[EmbedResult]) -> None:
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM vault_chunks WHERE entry_id = %s", (job.entry_id,))
        register_vector(conn)
        rows = [
            (
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
            INSERT INTO vault_chunks
              (entry_id, user_id, chunk_index, content, embedding, token_count, chunk_hash)
            VALUES (%s, %s, %s, %s, %s::vector, %s, %s)
            ON CONFLICT (entry_id, chunk_index) DO NOTHING
            """,
            rows,
        )
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
            (len(embeddings), settings.embedding_model, CHUNKER_VERSION, job.file_hash, job.entry_id),
        )

    if job.file_hash:
        mark_file_indexed(job.file_hash, job.entry_id)
