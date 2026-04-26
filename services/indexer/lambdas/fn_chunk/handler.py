"""
Lambda handler: fn-chunk

Pure computation -- no external service dependencies beyond tiktoken.

Reads extracted text (from SFN payload or S3 ref), applies kind-aware
chunking, and returns chunks array (or S3 ref if too large).

Input:  Event with extracted_text (inline) or text_s3_key (S3 ref)
Output: Event + { chunks: [...] | chunks_s3_key, chunk_count }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any

import boto3
import tiktoken

# ---------------------------------------------------------------------------
# Frozen constants (MUST match app/pipeline/chunk.py -- never change)
# ---------------------------------------------------------------------------

CHUNK_SIZE = 500
OVERLAP = 50
ENC = tiktoken.get_encoding("cl100k_base")

S3_DATA_BUS_THRESHOLD = 200_000  # bytes
PIPELINE_BUCKET = os.environ.get("S3_BUCKET", "vault-local")

# ---------------------------------------------------------------------------
# S3 client
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


# ---------------------------------------------------------------------------
# Chunk data class
# ---------------------------------------------------------------------------


@dataclass
class ChunkResult:
    chunk_index: int
    content: str
    token_count: int


# ---------------------------------------------------------------------------
# Chunking strategies (mirrored from app/pipeline/chunk.py)
# ---------------------------------------------------------------------------


def chunk_sliding(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = OVERLAP) -> list[ChunkResult]:
    tokens = ENC.encode(text)
    if not tokens:
        return []
    chunks: list[ChunkResult] = []
    i, idx = 0, 0
    while i < len(tokens):
        window = tokens[i : i + chunk_size]
        chunks.append(ChunkResult(chunk_index=idx, content=ENC.decode(window), token_count=len(window)))
        i += chunk_size - overlap
        idx += 1
    return chunks


def _split_code_blocks(text: str) -> list[str]:
    pattern = r"(?=^(?:def |class |function |fn |func |sub |procedure ))"
    blocks = re.split(pattern, text, flags=re.MULTILINE)
    return [b for b in blocks if b.strip()]


def chunk_code(text: str) -> list[ChunkResult]:
    blocks = _split_code_blocks(text)
    chunks: list[ChunkResult] = []
    idx = 0
    for block in blocks:
        toks = ENC.encode(block)
        if len(toks) <= CHUNK_SIZE:
            chunks.append(ChunkResult(chunk_index=idx, content=block.strip(), token_count=len(toks)))
            idx += 1
        else:
            for sub in chunk_sliding(block, CHUNK_SIZE, overlap=0):
                chunks.append(ChunkResult(chunk_index=idx, content=sub.content, token_count=sub.token_count))
                idx += 1
    return chunks


def chunk_document(text: str) -> list[ChunkResult]:
    paragraphs = text.split("\n\n")
    chunks: list[ChunkResult] = []
    current_parts: list[str] = []
    current_tokens = 0
    idx = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        para_tokens = len(ENC.encode(para))
        if para_tokens > CHUNK_SIZE:
            if current_parts:
                content = "\n\n".join(current_parts)
                chunks.append(ChunkResult(chunk_index=idx, content=content, token_count=current_tokens))
                idx += 1
                current_parts, current_tokens = [], 0
            for sub in chunk_sliding(para):
                chunks.append(ChunkResult(chunk_index=idx, content=sub.content, token_count=sub.token_count))
                idx += 1
        elif current_tokens + para_tokens > CHUNK_SIZE:
            content = "\n\n".join(current_parts)
            chunks.append(ChunkResult(chunk_index=idx, content=content, token_count=current_tokens))
            idx += 1
            current_parts, current_tokens = [para], para_tokens
        else:
            current_parts.append(para)
            current_tokens += para_tokens

    if current_parts:
        content = "\n\n".join(current_parts)
        chunks.append(ChunkResult(chunk_index=idx, content=content, token_count=current_tokens))

    return chunks


def chunk_data(text: str) -> list[ChunkResult]:
    lines = text.split("\n")
    header = ""
    data_lines: list[str] = []
    for line in lines:
        if line.lower().startswith("columns:") or line.lower().startswith("schema:"):
            header = line
        else:
            data_lines.append(line)

    chunks: list[ChunkResult] = []
    idx = 0
    if header:
        h_tokens = len(ENC.encode(header))
        chunks.append(ChunkResult(chunk_index=idx, content=header, token_count=h_tokens))
        idx += 1

    batch: list[str] = []
    batch_tokens = 0
    for line in data_lines:
        line = line.strip()
        if not line:
            continue
        lt = len(ENC.encode(line))
        if batch_tokens + lt > CHUNK_SIZE and batch:
            content = "\n".join(batch)
            chunks.append(ChunkResult(chunk_index=idx, content=content, token_count=batch_tokens))
            idx += 1
            batch, batch_tokens = [], 0
        batch.append(line)
        batch_tokens += lt

    if batch:
        content = "\n".join(batch)
        chunks.append(ChunkResult(chunk_index=idx, content=content, token_count=batch_tokens))

    return chunks


def chunk_single(text: str) -> list[ChunkResult]:
    tokens = ENC.encode(text)
    return [ChunkResult(chunk_index=0, content=text.strip(), token_count=len(tokens))]


# Dispatch table
CHUNK_DISPATCH = {
    "document": chunk_document,
    "note": chunk_document,
    "web": chunk_document,
    "code": chunk_code,
    "snippet": chunk_code,
    "data": chunk_data,
    "config": chunk_data,
    "image": chunk_single,
    "design": chunk_single,
}


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict, context: Any) -> dict:
    entry_id = event["entry_id"]
    kind = event.get("kind", "unsorted")

    # 1. Read extracted text (from SFN payload or S3 ref)
    if "text_s3_key" in event:
        data = _read_s3_json(event["text_s3_key"])
        extracted_text = data["text"]
    else:
        extracted_text = event.get("extracted_text", "")

    if not extracted_text.strip():
        raise RuntimeError(json.dumps({"code": "EMPTY_TEXT", "message": "Cannot chunk empty text"}))

    # 2. Chunk using kind-dispatched strategy
    chunker = CHUNK_DISPATCH.get(kind, chunk_sliding)
    chunks = chunker(extracted_text)

    # Re-index sequentially
    for i, c in enumerate(chunks):
        c.chunk_index = i

    # 3. Build output
    chunks_data = [asdict(c) for c in chunks]
    result = {
        "entry_id": entry_id,
        "user_id": event.get("user_id", ""),
        "bucket": event.get("bucket", ""),
        "s3_key": event.get("s3_key", ""),
        "file_name": event.get("file_name", ""),
        "kind": kind,
        "subkind": event.get("subkind", ""),
        "file_hash": event.get("file_hash", ""),
        "chunk_count": len(chunks),
    }

    payload_bytes = json.dumps(chunks_data).encode("utf-8")
    if len(payload_bytes) > S3_DATA_BUS_THRESHOLD:
        chunks_s3_key = f"pipeline/{entry_id}/chunks.json"
        _get_s3().put_object(
            Bucket=PIPELINE_BUCKET,
            Key=chunks_s3_key,
            Body=payload_bytes,
            ContentType="application/json",
        )
        result["chunks_s3_key"] = chunks_s3_key
    else:
        result["chunks"] = chunks_data

    # Clean up text_s3_key reference (no longer needed downstream)
    # Keep it in result so Store can clean up intermediate files
    if "text_s3_key" in event:
        result["text_s3_key"] = event["text_s3_key"]

    return result
