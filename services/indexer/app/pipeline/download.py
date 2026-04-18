from app.types import PipelineError

from pathlib import Path

from app.cache.hashing import file_hash
from app.cache.redis_cache import is_file_indexed
from app.db.connection import execute_query
from app.s3.rate_limiter import rate_limited_s3
from app.types import PipelineError, PipelineJob


class FileAlreadyIndexed(PipelineError):
    def __init__(self, msg: str):
        super().__init__(msg, "ALREADY_INDEXED")


def download(job: PipelineJob) -> tuple[str, str]:
    local_path = f"/tmp/{job.entry_id}_{Path(job.file_name).name}"
    rate_limited_s3.download(job.bucket, job.s3_key, local_path)
    fhash = file_hash(local_path)
    job.file_hash = fhash  # expose hash to caller even if we short-circuit
    if is_file_indexed(fhash):
        raise FileAlreadyIndexed(f"File already indexed (hash={fhash})")

    # DB-backed dedupe for resilience after Redis eviction/restart.
    existing = execute_query(
        """
        SELECT entry_id
        FROM vault_entries
        WHERE file_hash = %s AND index_status = 'indexed' AND entry_id <> %s
        LIMIT 1
        """,
        (fhash, job.entry_id),
    )
    if existing:
        raise FileAlreadyIndexed(f"File already indexed (hash={fhash})")
    return local_path, fhash
