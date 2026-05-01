from pathlib import Path

from app.s3.client import head_object
from app.types import EntryKind, PipelineError, PipelineJob

ALLOWED_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/markdown",
    "text/plain",
    "text/html",
    "text/csv",
    "application/json",
    "application/x-yaml",
    "application/toml",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/svg+xml",
    "application/zip",
    "application/x-zip-compressed",
}

CODE_EXTENSIONS = {
    ".rs",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".py",
    ".go",
    ".rb",
    ".java",
    ".kt",
    ".swift",
    ".c",
    ".cpp",
    ".h",
    ".sql",
    ".sh",
    ".lua",
}


def validate(job: PipelineJob) -> None:
    if job.kind != EntryKind.UNSORTED and job.mime not in ALLOWED_MIMES:
        ext = Path(job.file_name).suffix.lower()
        if not (job.kind in {EntryKind.CODE, EntryKind.SNIPPET} and ext in CODE_EXTENSIONS):
            raise PipelineError(f"Unsupported file type: {job.mime}", "INVALID_TYPE")

    if job.size_bytes > 50 * 1024 * 1024:
        raise PipelineError("File too large (max 50 MB)", "FILE_TOO_LARGE")

    meta = head_object(job.bucket, job.s3_key)
    if meta is None:
        raise PipelineError(f"S3 object not found: {job.s3_key}", "S3_NOT_FOUND")
