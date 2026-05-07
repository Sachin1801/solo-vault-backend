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

import csv
import hashlib
import json
import os
import re
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
# Parse dispatcher.
#
# Keep this Lambda standalone. Importing app.pipeline.parse pulls app.config,
# which validates local DB/MinIO settings that are intentionally absent in AWS.
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    normalized = "\n".join(lines)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def _read_text(local_path: str) -> str:
    path = Path(local_path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1", errors="replace")


def _parse_pdf(local_path: str) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(local_path)
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
        text = "\n\n".join(page for page in pages if page)
        if text:
            return text
    except Exception as exc:
        print(json.dumps({
            "event": "vault.pipeline.download_parse.pdf_extract_failed",
            "file_name": Path(local_path).name,
            "error": str(exc),
        }))
    return f"[PDF text extraction produced no readable text: {Path(local_path).name}]"


def _parse_docx(local_path: str) -> str:
    from docx import Document

    doc = Document(local_path)
    return "\n".join(
        paragraph.text.strip()
        for paragraph in doc.paragraphs
        if paragraph.text and paragraph.text.strip()
    )


def _rows_to_lines(headers: list[str], rows: list[list[str]]) -> str:
    lines: list[str] = []
    for row in rows:
        values = [row[i] if i < len(row) else "" for i in range(len(headers))]
        lines.append(", ".join(f"{header}={value}" for header, value in zip(headers, values)))
    return "\n".join(lines)


def _parse_data(local_path: str) -> str:
    path = Path(local_path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.reader(f))
        if not rows:
            return ""
        headers = rows[0]
        return f"Columns: {', '.join(headers)}\n\nSample rows:\n{_rows_to_lines(headers, rows[1:101])}"
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            keys = list(data.keys())
            sample = {key: data[key] for key in keys[:3]}
            return f"JSON keys: {', '.join(keys)}\nSample: {json.dumps(sample, ensure_ascii=False)}"
        return json.dumps(data[:3] if isinstance(data, list) else data, ensure_ascii=False)
    if suffix in {".yaml", ".yml"}:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            keys = list(data.keys())
            sample = {key: data[key] for key in keys[:3]}
            return f"YAML keys: {', '.join(keys)}\nSample: {json.dumps(sample, ensure_ascii=False)}"
        return str(data)
    return _read_text(local_path)


def _parse_html(local_path: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_read_text(local_path), "html.parser")
    return soup.get_text("\n")


def _parse_code(local_path: str) -> str:
    path = Path(local_path)
    return f"File: {path.name}\n\n{_read_text(local_path)}"


def _parse(kind: str, local_path: str) -> str:
    path = Path(local_path)
    suffix = path.suffix.lower()
    parser = "text"
    try:
        if suffix == ".pdf":
            parser = "pdf"
            text = _parse_pdf(local_path)
        elif suffix == ".docx":
            parser = "docx"
            text = _parse_docx(local_path)
        elif suffix in {".csv", ".json", ".yaml", ".yml", ".toml"}:
            parser = "data"
            text = _parse_data(local_path)
        elif suffix in {".html", ".htm"}:
            parser = "html"
            text = _parse_html(local_path)
        elif suffix in {
            ".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java", ".kt",
            ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".m", ".mm", ".rb",
            ".php", ".cs", ".sh", ".zsh", ".sql", ".css", ".scss", ".md",
            ".txt", ".rtf", ".log", ".env", ".ini",
        }:
            parser = "code" if kind in {"code", "snippet"} else "text"
            text = _parse_code(local_path) if parser == "code" else _read_text(local_path)
        else:
            parser = "fallback"
            text = _read_text(local_path)
    except Exception as exc:
        print(json.dumps({
            "event": "vault.pipeline.download_parse.parse_failed",
            "file_name": path.name,
            "kind": kind,
            "parser": parser,
            "error": str(exc),
        }))
        text = f"[File could not be parsed as text: {path.name}]"
    print(json.dumps({
        "event": "vault.pipeline.download_parse.parser_selected",
        "file_name": path.name,
        "kind": kind,
        "parser": parser,
    }))
    return _normalize(text)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    entry_id = event["entry_id"]
    bucket = event["bucket"]
    s3_key = event["s3_key"]
    file_name = event["file_name"]
    kind = event.get("kind", "unsorted")
    print(json.dumps({
        "event": "vault.pipeline.download_parse.started",
        "entry_id": entry_id,
        "user_id": event.get("user_id", ""),
        "bucket": bucket,
        "s3_key": s3_key,
        "file_name": file_name,
        "kind": kind,
    }))

    # 1. Download
    local_path, fhash = _download(bucket, s3_key, entry_id, file_name)
    print(json.dumps({
        "event": "vault.pipeline.download_parse.downloaded",
        "entry_id": entry_id,
        "user_id": event.get("user_id", ""),
        "s3_key": s3_key,
        "file_hash": fhash,
    }))

    # 2. Parse
    extracted_text = _parse(kind, local_path)
    print(json.dumps({
        "event": "vault.pipeline.download_parse.parsed",
        "entry_id": entry_id,
        "user_id": event.get("user_id", ""),
        "text_chars": len(extracted_text),
    }))

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
        print(json.dumps({
            "event": "vault.pipeline.download_parse.text_stored",
            "entry_id": entry_id,
            "user_id": event.get("user_id", ""),
            "text_s3_key": text_s3_key,
            "text_bytes": len(text_bytes),
        }))
    else:
        result["extracted_text"] = extracted_text

    print(json.dumps({
        "event": "vault.pipeline.download_parse.completed",
        "entry_id": entry_id,
        "user_id": event.get("user_id", ""),
        "file_hash": fhash,
        "text_bytes": len(text_bytes),
    }))
    return result
