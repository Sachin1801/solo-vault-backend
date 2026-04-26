"""
Lambda handler: fn-download-parse

Downloads file from S3, computes SHA-256 hash for dedup, then parses
extracted text using the kind-dispatched parser.

If extracted_text > 200 KB: writes to S3 intermediate storage and
returns an S3 reference in the event (S3 Data Bus pattern).

Input:  Step Functions event (validated PipelineJob fields)
Output: Event + { file_hash, extracted_text | text_s3_key }
Errors: ALREADY_INDEXED (caught by SFN -> CloneFromSource)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Add the app package to sys.path so we can import pipeline modules.
# In the container image, app/ is at /app/ alongside this handler.
# ---------------------------------------------------------------------------
APP_ROOT = os.environ.get("APP_ROOT", "/app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)

# ---------------------------------------------------------------------------
# S3 client
# ---------------------------------------------------------------------------

_s3 = None
PIPELINE_BUCKET = os.environ.get("S3_BUCKET", "vault-local")
S3_DATA_BUS_THRESHOLD = 200_000  # bytes


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


# ---------------------------------------------------------------------------
# Hashing (inline — avoid importing app.cache.hashing to keep this standalone)
# ---------------------------------------------------------------------------


def _file_hash(local_path: str) -> str:
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def _download(bucket: str, key: str, entry_id: str, file_name: str) -> tuple[str, str]:
    """Download file from S3 to /tmp, return (local_path, file_hash)."""
    safe_name = Path(file_name).name
    local_path = f"/tmp/{entry_id}_{safe_name}"
    _get_s3().download_file(bucket, key, local_path)
    fhash = _file_hash(local_path)
    return local_path, fhash


# ---------------------------------------------------------------------------
# Parse dispatcher (import from app package if available, else minimal fallback)
# ---------------------------------------------------------------------------


def _parse(kind: str, local_path: str) -> str:
    """Try to import the full parser from the app package. Fall back to raw text."""
    try:
        from app.pipeline.parse import parse as full_parse
        from app.types import PipelineJob

        # Build a minimal PipelineJob for the parser dispatcher
        job = PipelineJob(
            job_id="",
            entry_id="",
            user_id="",
            s3_key="",
            bucket="",
            file_name=Path(local_path).name,
            mime="",
            kind=kind,
            subkind="",
            size_bytes=0,
        )
        return full_parse(job, local_path)
    except ImportError:
        # Minimal fallback: raw text read
        try:
            return Path(local_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "[Binary file]"


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    entry_id = event["entry_id"]
    bucket = event["bucket"]
    s3_key = event["s3_key"]
    file_name = event["file_name"]
    kind = event.get("kind", "unsorted")

    # 1. Download
    local_path, fhash = _download(bucket, s3_key, entry_id, file_name)

    # 2. Parse
    extracted_text = _parse(kind, local_path)

    # 3. Clean up /tmp
    try:
        os.unlink(local_path)
    except OSError:
        pass

    # 4. Build output — S3 data bus if text is too large for SFN payload
    result = {**event, "file_hash": fhash}

    text_bytes = extracted_text.encode("utf-8")
    if len(text_bytes) > S3_DATA_BUS_THRESHOLD:
        text_s3_key = f"pipeline/{entry_id}/text.json"
        _get_s3().put_object(
            Bucket=PIPELINE_BUCKET,
            Key=text_s3_key,
            Body=json.dumps({"text": extracted_text}).encode("utf-8"),
            ContentType="application/json",
        )
        result["text_s3_key"] = text_s3_key
    else:
        result["extracted_text"] = extracted_text

    return result
