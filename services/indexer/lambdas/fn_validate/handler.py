"""
Lambda handler: fn-validate

Validates an indexing job before processing:
  - MIME type check against allowlist
  - File size check (max 50 MB)
  - S3 HEAD to verify object exists

Input:  Step Functions event (PipelineJob fields as JSON)
Output: Same event (pass-through on success)
Errors: PipelineError with codes INVALID_TYPE, FILE_TOO_LARGE, S3_NOT_FOUND
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Constants (mirrored from app.pipeline.validate — keep in sync)
# ---------------------------------------------------------------------------

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
    ".rs", ".ts", ".tsx", ".js", ".jsx", ".py", ".go", ".rb",
    ".java", ".kt", ".swift", ".c", ".cpp", ".h", ".sql", ".sh", ".lua",
}

CODE_KINDS = {"code", "snippet"}

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

# ---------------------------------------------------------------------------
# S3 client (lazy singleton — survives Lambda warm invocations)
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


def _head_object(bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return _get_s3().head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------


class PipelineError(Exception):
    def __init__(self, message: str, code: str = "PIPELINE_ERROR"):
        super().__init__(message)
        self.code = code


def validate(event: dict) -> None:
    kind = event.get("kind", "unsorted")
    mime = event.get("mime", "")
    file_name = event.get("file_name", "")
    size_bytes = event.get("size_bytes", 0)
    bucket = event["bucket"]
    s3_key = event["s3_key"]

    # MIME check (UNSORTED bypasses)
    if kind != "unsorted" and mime not in ALLOWED_MIMES:
        ext = Path(file_name).suffix.lower()
        if not (kind in CODE_KINDS and ext in CODE_EXTENSIONS):
            raise PipelineError(f"Unsupported file type: {mime}", "INVALID_TYPE")

    # Size check
    if size_bytes > MAX_FILE_BYTES:
        raise PipelineError("File too large (max 50 MB)", "FILE_TOO_LARGE")

    # S3 existence check
    meta = _head_object(bucket, s3_key)
    if meta is None:
        raise PipelineError(f"S3 object not found: {s3_key}", "S3_NOT_FOUND")


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    """Step Functions invokes this. Returns the event on success, raises on failure."""
    try:
        validate(event)
        return event
    except PipelineError as exc:
        # Step Functions catches errors by their name.  We raise a RuntimeError
        # whose string the ASL Catch block can match via ErrorEquals.
        raise RuntimeError(json.dumps({"code": exc.code, "message": str(exc)}))
