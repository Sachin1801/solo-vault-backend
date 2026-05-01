#!/usr/bin/env python3
"""
Upload the prepared dataset (808 SEC filing documents) to S3/MinIO.

Reads manifest.jsonl to get file paths and S3 key mappings,
then uploads each file to the target bucket.

Usage:
  # Upload to local MinIO (default)
  python scripts/upload_dataset.py

  # Upload to real AWS S3
  python scripts/upload_dataset.py --bucket solo-vault-dev --no-endpoint

  # Upload specific group only
  python scripts/upload_dataset.py --group dirty --limit 10

  # Upload and trigger indexing via Step Functions
  python scripts/upload_dataset.py --trigger --sfn-arn arn:aws:states:...

Environment variables:
  S3_ENDPOINT_URL  - MinIO URL (default: http://localhost:9000)
  S3_ACCESS_KEY    - Access key (default: minioadmin)
  S3_SECRET_KEY    - Secret key (default: minioadmin)
  S3_REGION        - Region (default: us-east-1)
  DATASET_PATH     - Path to dataset_all/ directory
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_DATASET_PATH = os.path.expanduser(
    "~/Desktop/Uni/cloud-computing/corp_dataset_pipeline/dataset_all"
)
DEFAULT_BUCKET = "vault-local"
DEFAULT_ENDPOINT = "http://localhost:9000"

# Map format groups to EntryKind + subkind for indexing triggers
FORMAT_MAP = {
    "dirty_pdf": {"kind": "document", "subkind": "pdf", "mime": "application/pdf"},
    "clean_pdf": {"kind": "document", "subkind": "pdf", "mime": "application/pdf"},
    "pdf_native": {"kind": "document", "subkind": "pdf", "mime": "application/pdf"},
    "docx": {
        "kind": "document",
        "subkind": "docx",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    },
    "xlsx": {
        "kind": "data",
        "subkind": "xlsx",
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
    "email": {"kind": "unsorted", "subkind": "eml", "mime": "message/rfc822"},
    "eml": {"kind": "unsorted", "subkind": "eml", "mime": "message/rfc822"},
    "images": {"kind": "image", "subkind": "png", "mime": "image/png"},
    "png": {"kind": "image", "subkind": "png", "mime": "image/png"},
}


def get_s3_client(endpoint: str | None, access_key: str, secret_key: str, region: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
    )


def ensure_bucket(s3, bucket: str) -> None:
    """Create bucket if it doesn't exist (MinIO only)."""
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        try:
            s3.create_bucket(Bucket=bucket)
            print(f"  Created bucket: {bucket}")
        except ClientError as e:
            print(f"  Warning: could not create bucket {bucket}: {e}")


def load_manifest(dataset_path: str) -> list[dict]:
    manifest_path = Path(dataset_path) / "manifest" / "manifest.jsonl"
    if not manifest_path.exists():
        print(f"Error: manifest not found at {manifest_path}")
        sys.exit(1)

    records = []
    with open(manifest_path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    return records


def resolve_local_path(dataset_path: str, record: dict) -> Path | None:
    """Find the actual file on disk from a manifest record."""
    # Try the 'file' field directly
    file_rel = record.get("file", "")
    candidates = [
        Path(dataset_path) / file_rel,
        Path(dataset_path) / record.get("format", "") / Path(file_rel).name,
    ]

    # Try in 03_synthetic subdirectories
    fmt = record.get("format", "")
    name = Path(file_rel).name
    synthetic_base = Path(dataset_path) / "03_synthetic"
    for subdir in ["clean_pdf", "docx", "xlsx", "email", "images"]:
        candidates.append(synthetic_base / subdir / name)

    # Try in 00_dirty_pdf
    candidates.append(Path(dataset_path) / "00_dirty_pdf" / name)

    for p in candidates:
        if p.exists() and p.is_file():
            return p

    return None


def upload_file(s3, bucket: str, local_path: Path, s3_key: str) -> bool:
    try:
        s3.upload_file(str(local_path), bucket, s3_key)
        return True
    except Exception as e:
        print(f"  Error uploading {s3_key}: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Upload dataset to S3/MinIO")
    parser.add_argument("--dataset", default=os.environ.get("DATASET_PATH", DEFAULT_DATASET_PATH))
    parser.add_argument("--bucket", default=os.environ.get("S3_BUCKET", DEFAULT_BUCKET))
    parser.add_argument("--endpoint", default=os.environ.get("S3_ENDPOINT_URL", DEFAULT_ENDPOINT))
    parser.add_argument("--no-endpoint", action="store_true", help="Use real AWS S3 (no endpoint)")
    parser.add_argument("--group", help="Only upload files of this format group")
    parser.add_argument("--limit", type=int, help="Max files to upload")
    parser.add_argument("--trigger", action="store_true", help="Trigger indexing after upload")
    parser.add_argument("--sfn-arn", help="Step Functions state machine ARN (for --trigger)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be uploaded")
    args = parser.parse_args()

    endpoint = None if args.no_endpoint else args.endpoint
    access_key = os.environ.get("S3_ACCESS_KEY", "minioadmin")
    secret_key = os.environ.get("S3_SECRET_KEY", "minioadmin")
    region = os.environ.get("S3_REGION", "us-east-1")

    s3 = get_s3_client(endpoint, access_key, secret_key, region)

    print(f"Dataset:  {args.dataset}")
    print(f"Bucket:   {args.bucket}")
    print(f"Endpoint: {endpoint or 'AWS S3'}")
    print()

    # Ensure bucket exists (MinIO)
    if endpoint:
        ensure_bucket(s3, args.bucket)

    # Load manifest
    records = load_manifest(args.dataset)
    print(f"Manifest: {len(records)} records")

    # Filter by group
    if args.group:
        records = [r for r in records if r.get("format", "") == args.group]
        print(f"Filtered to group '{args.group}': {len(records)} records")

    # Limit
    if args.limit:
        records = records[: args.limit]

    # Upload
    uploaded = 0
    skipped = 0
    failed = 0
    t0 = time.time()

    for i, record in enumerate(records):
        s3_key = record.get("minio_key", "")
        if not s3_key:
            skipped += 1
            continue

        local_path = resolve_local_path(args.dataset, record)
        if local_path is None:
            skipped += 1
            continue

        if args.dry_run:
            print(f"  [{i+1}/{len(records)}] {local_path.name} -> s3://{args.bucket}/{s3_key}")
            uploaded += 1
            continue

        if upload_file(s3, args.bucket, local_path, s3_key):
            uploaded += 1
            if (i + 1) % 50 == 0:
                print(f"  [{i+1}/{len(records)}] uploaded {uploaded} files...")
        else:
            failed += 1

    elapsed = time.time() - t0
    print()
    print(f"Done in {elapsed:.1f}s: {uploaded} uploaded, {skipped} skipped, {failed} failed")

    # Trigger indexing
    if args.trigger and args.sfn_arn and not args.dry_run:
        print()
        print(f"Triggering indexing via Step Functions: {args.sfn_arn}")
        sfn = boto3.client("stepfunctions", region_name=region)
        triggered = 0
        for record in records:
            s3_key = record.get("minio_key", "")
            if not s3_key:
                continue
            fmt = record.get("format", "")
            meta = FORMAT_MAP.get(fmt, {"kind": "unsorted", "subkind": "", "mime": "application/octet-stream"})
            local_path = resolve_local_path(args.dataset, record)
            size_bytes = local_path.stat().st_size if local_path else 0

            input_data = {
                "entry_id": record.get("id", s3_key),
                "user_id": "dataset-upload",
                "project_id": None,
                "s3_key": s3_key,
                "bucket": args.bucket,
                "file_name": Path(s3_key).name,
                "mime": meta["mime"],
                "kind": meta["kind"],
                "subkind": meta["subkind"],
                "size_bytes": size_bytes,
                "title": record.get("id", Path(s3_key).stem),
                "tags": ["dataset", fmt],
                "classifier_confidence": 0.99,
            }

            try:
                sfn.start_execution(
                    stateMachineArn=args.sfn_arn,
                    name=f"dataset-{record.get('id', s3_key).replace('/', '-')}",
                    input=json.dumps(input_data),
                )
                triggered += 1
            except Exception as e:
                print(f"  Failed to trigger {s3_key}: {e}")

        print(f"Triggered {triggered} Step Functions executions")


if __name__ == "__main__":
    main()
