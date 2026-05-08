from app.pipeline.download import FileAlreadyIndexed
from app.db.connection import execute_write, transaction
from app.notify.progress import emit_progress
from app.pipeline.chunk import chunk
from app.pipeline.download import download
from app.pipeline.embed import embed
from app.pipeline.parse import parse
from app.pipeline.store import store
from app.pipeline.validate import validate
from app.types import PipelineError, PipelineJob
from app.workers.celery_app import celery_app


def update_status(entry_id: str, status: str) -> None:
    execute_write(
        """
        UPDATE vault.entries
        SET index_status = %s,
            cloud_sync_state = CASE
                WHEN %s = 'indexed' THEN 'synced'
                WHEN %s = 'failed' THEN 'failed'
                ELSE cloud_sync_state
            END,
            indexed_at = CASE
                WHEN %s = 'indexed' THEN EXTRACT(EPOCH FROM NOW())::bigint
                ELSE indexed_at
            END,
            updated_at = EXTRACT(EPOCH FROM NOW())::bigint
        WHERE id = %s
        """,
        (status, status, status, status, entry_id),
    )


def purge_entry_vectors(entry_id: str) -> None:
    execute_write("DELETE FROM vault.chunks_fts WHERE entry_id = %s", (entry_id,))
    execute_write("DELETE FROM vault.chunks WHERE entry_id = %s", (entry_id,))


def _clone_from_source(job: PipelineJob) -> None:
    """Copy chunks + metadata from an already-indexed entry with the same file hash.

    Called when FileAlreadyIndexed short-circuits the pipeline so that the
    new entry ends up with valid chunks (under the correct user_id) and
    populated metadata columns.
    """
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, chunker_version, embedding_model, chunk_count
            FROM vault.entries
            WHERE file_hash = %s AND user_id = %s AND index_status = 'indexed' AND id <> %s
            LIMIT 1
            """,
            (job.file_hash, job.user_id, job.entry_id),
        )
        source = cur.fetchone()
        if source is None:
            return
        src_entry_id, chunker_ver, emb_model, chunk_count = source

        # Remove any stale chunks for this entry first
        cur.execute("DELETE FROM vault.chunks_fts WHERE entry_id = %s", (job.entry_id,))
        cur.execute("DELETE FROM vault.chunks WHERE entry_id = %s", (job.entry_id,))

        # Copy chunks, substituting this entry's id and user_id
        cur.execute(
            """
            INSERT INTO vault.chunks
              (id, entry_id, user_id, chunk_index, content, embedding, token_count, chunk_hash)
            SELECT %s || ':' || chunk_index::text, %s, %s, chunk_index, content, embedding, token_count, chunk_hash
            FROM vault.chunks
            WHERE entry_id = %s
            ON CONFLICT (entry_id, chunk_index) DO NOTHING
            """,
            (job.entry_id, job.entry_id, job.user_id, src_entry_id),
        )
        cur.execute(
            """
            INSERT INTO vault.chunks_fts (content, entry_id, chunk_id, user_id)
            SELECT content, entry_id, id, user_id
            FROM vault.chunks
            WHERE entry_id = %s
            """,
            (job.entry_id,),
        )

        cur.execute(
            """
            UPDATE vault.entries
            SET chunker_version = %s,
                embedding_model  = %s,
                file_hash        = %s,
                chunk_count      = %s,
                index_status     = 'indexed',
                cloud_sync_state = 'synced',
                index_error      = NULL,
                indexed_at       = EXTRACT(EPOCH FROM NOW())::bigint,
                updated_at       = EXTRACT(EPOCH FROM NOW())::bigint
            WHERE id = %s AND user_id = %s
            """,
            (chunker_ver, emb_model, job.file_hash, chunk_count, job.entry_id, job.user_id),
        )


@celery_app.task(bind=True, max_retries=3, default_retry_delay=5)
def run_pipeline(self, job_dict: dict) -> dict:
    job = PipelineJob(**job_dict)
    try:
        update_status(job.entry_id, "extracting")

        emit_progress(job.entry_id, "validate", pct=10, message="Validating")
        validate(job)
        emit_progress(job.entry_id, "validate", pct=10, message="Validated")

        emit_progress(job.entry_id, "download", pct=25, message="Downloading")
        local_path, file_hash = download(job)
        job.file_hash = file_hash
        emit_progress(job.entry_id, "download", pct=25, message="Downloaded")

        emit_progress(job.entry_id, "parse", pct=50, message="Parsing")
        extracted_text = parse(job, local_path)
        emit_progress(job.entry_id, "parse", pct=50, message="Parsed")

        emit_progress(job.entry_id, "chunk", pct=60, message="Chunking")
        chunks = chunk(job, extracted_text)
        emit_progress(job.entry_id, "chunk", pct=60, message="Chunked")

        emit_progress(job.entry_id, "embed", pct=80, message="Embedding")
        embeddings = embed(chunks)
        emit_progress(job.entry_id, "embed", pct=80, message="Embedded")

        emit_progress(job.entry_id, "store", pct=90, message="Storing")
        store(job, embeddings)
        emit_progress(job.entry_id, "store", pct=90, message="Stored")

        emit_progress(job.entry_id, "done", pct=100, message="Completed")
        update_status(job.entry_id, "indexed")
        return {"status": "completed", "entry_id": job.entry_id}
    except FileAlreadyIndexed as exc:
        _clone_from_source(job)
        emit_progress(job.entry_id, "done", pct=100, message=str(exc))
        update_status(job.entry_id, "indexed")
        return {"status": "completed", "entry_id": job.entry_id}
    except PipelineError as exc:
        if exc.code == "S3_NOT_FOUND":
            purge_entry_vectors(job.entry_id)
            update_status(job.entry_id, "failed")
            emit_progress(job.entry_id, "failed", pct=0, message=str(exc))
            return {"status": "failed", "entry_id": job.entry_id}
        update_status(job.entry_id, "failed")
        emit_progress(job.entry_id, "failed", pct=0, message=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)
