#!/usr/bin/env python3
"""
Bulk index files from MinIO vault-test bucket.

Usage:
    python scripts/bulk_index.py [--group dirty|pdf_native|docx|xlsx|eml|png|all]
                                  [--limit N]
                                  [--concurrency N]
                                  [--api http://localhost:8000]
                                  [--user u1]
                                  [--no-wait]

Output:
    Live progress table → final summary with per-group stats.
"""

import argparse
import concurrent.futures
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import os

import boto3
import httpx

# ── Dataset config ────────────────────────────────────────────────────────────

MINIO_ENDPOINT = os.getenv("S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_ACCESS   = os.getenv("S3_ACCESS_KEY", "minioadmin")
MINIO_SECRET   = os.getenv("S3_SECRET_KEY", "minioadmin")
BUCKET         = "vault-test"

GROUP_CONFIG = {
    "dirty": {
        "prefix": "dirty/",
        "mime": "application/pdf",
        "kind": "document",
        "subkind": "pdf",
    },
    "pdf_native": {
        "prefix": "pdf_native/",
        "mime": "application/pdf",
        "kind": "document",
        "subkind": "pdf",
    },
    "docx": {
        "prefix": "docx/",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "kind": "document",
        "subkind": "docx",
    },
    "xlsx": {
        "prefix": "xlsx/",
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "kind": "data",
        "subkind": "xlsx",
    },
    "eml": {
        "prefix": "eml/",
        "mime": "message/rfc822",
        "kind": "note",
        "subkind": "eml",
    },
    "png": {
        "prefix": "png/",
        "mime": "image/png",
        "kind": "image",
        "subkind": "png",
    },
}

# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Job:
    entry_id: str
    group: str
    s3_key: str
    size_bytes: int
    submitted_at: float = 0.0
    finished_at: float = 0.0
    final_status: str = "pending"
    chunk_count: int = 0
    error: str = ""

# ── S3 helpers ────────────────────────────────────────────────────────────────

def list_objects(s3, group: str, limit: int) -> list[dict]:
    cfg = GROUP_CONFIG[group]
    paginator = s3.get_paginator("list_objects_v2")
    objects = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=cfg["prefix"]):
        for obj in page.get("Contents", []):
            objects.append(obj)
            if len(objects) >= limit:
                return objects
    return objects

# ── API helpers ───────────────────────────────────────────────────────────────

def submit_job(api: str, user: str, group: str, obj: dict) -> Job:
    cfg = GROUP_CONFIG[group]
    key = obj["Key"]
    entry_id = f"{group}-{uuid.uuid4().hex[:8]}"
    payload = {
        "entry_id": entry_id,
        "user_id": user,
        "project_id": None,
        "s3_key": key,
        "bucket": BUCKET,
        "file_name": key.split("/")[-1],
        "mime": cfg["mime"],
        "kind": cfg["kind"],
        "subkind": cfg["subkind"],
        "size_bytes": obj["Size"],
        "title": key.split("/")[-1],
        "tags": [group],
        "classifier_confidence": 0.95,
        "pinned": False,
        "memory_type": "",
    }
    resp = httpx.post(f"{api}/index", json=payload, timeout=10)
    resp.raise_for_status()
    job = Job(
        entry_id=entry_id,
        group=group,
        s3_key=key,
        size_bytes=obj["Size"],
        submitted_at=time.time(),
    )
    return job


def poll_job(api: str, job: Job) -> None:
    deadline = time.time() + 1800  # 30-min timeout per job
    while time.time() < deadline:
        try:
            r = httpx.get(f"{api}/jobs/{job.entry_id}", timeout=5)
            if r.status_code == 200:
                data = r.json()
                st = data.get("status", "")
                if st in ("indexed", "failed", "deleted"):
                    job.final_status = st
                    job.chunk_count = data.get("chunk_count", 0)
                    job.finished_at = time.time()
                    return
        except Exception:
            pass
        time.sleep(5)
    job.final_status = "timeout"
    job.finished_at = time.time()

# ── Reporting ─────────────────────────────────────────────────────────────────

def print_summary(jobs: list[Job]) -> None:
    from collections import defaultdict

    print("\n" + "=" * 72)
    print(f"{'BULK INDEX SUMMARY':^72}")
    print("=" * 72)

    by_group: dict[str, list[Job]] = defaultdict(list)
    for j in jobs:
        by_group[j.group].append(j)

    header = f"{'Group':<12} {'Total':>6} {'OK':>6} {'Fail':>6} {'Timeout':>8} {'Chunks':>8} {'Avg s':>8} {'P95 s':>8}"
    print(header)
    print("-" * 72)

    all_durations = []
    for group, gjobs in sorted(by_group.items()):
        total   = len(gjobs)
        ok      = sum(1 for j in gjobs if j.final_status == "indexed")
        fail    = sum(1 for j in gjobs if j.final_status == "failed")
        timeout = sum(1 for j in gjobs if j.final_status == "timeout")
        chunks  = sum(j.chunk_count for j in gjobs)
        durs    = [j.finished_at - j.submitted_at for j in gjobs if j.finished_at > 0]
        all_durations.extend(durs)
        avg_s = (sum(durs) / len(durs)) if durs else 0
        p95_s = sorted(durs)[int(len(durs) * 0.95)] if durs else 0
        print(f"{group:<12} {total:>6} {ok:>6} {fail:>6} {timeout:>8} {chunks:>8} {avg_s:>8.1f} {p95_s:>8.1f}")

    print("-" * 72)
    total_ok = sum(1 for j in jobs if j.final_status == "indexed")
    all_dur_s = f"{sum(all_durations) / len(all_durations):.1f}" if all_durations else "n/a"
    print(f"{'TOTAL':<12} {len(jobs):>6} {total_ok:>6}")
    print(f"\nOverall avg latency : {all_dur_s}s")
    print(f"Completed at        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)

    # Failures detail
    failures = [j for j in jobs if j.final_status != "indexed"]
    if failures:
        print(f"\n{'FAILED / TIMEOUT JOBS':^72}")
        print("-" * 72)
        for j in failures[:20]:
            print(f"  [{j.final_status}] {j.group}/{j.s3_key.split('/')[-1]}  entry={j.entry_id}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk index files from vault-test bucket")
    parser.add_argument("--group", default="all",
                        help="Group to index: dirty|pdf_native|docx|xlsx|eml|png|all")
    parser.add_argument("--limit", type=int, default=10,
                        help="Max files per group (default 10)")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="Parallel submit threads (default 4)")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--user", default="bench-user")
    parser.add_argument("--no-wait", action="store_true",
                        help="Submit jobs but don't poll for completion")
    args = parser.parse_args()

    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS,
        aws_secret_access_key=MINIO_SECRET,
        region_name="us-east-1",
    )

    groups = list(GROUP_CONFIG.keys()) if args.group == "all" else [args.group]

    # Collect objects
    all_objects: list[tuple[str, dict]] = []
    for group in groups:
        objs = list_objects(s3, group, args.limit)
        print(f"  {group}: {len(objs)} files queued")
        all_objects.extend((group, obj) for obj in objs)

    print(f"\nSubmitting {len(all_objects)} jobs (concurrency={args.concurrency})...")

    jobs: list[Job] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = {
            ex.submit(submit_job, args.api, args.user, group, obj): (group, obj)
            for group, obj in all_objects
        }
        for fut in concurrent.futures.as_completed(futs):
            try:
                job = fut.result()
                jobs.append(job)
                print(f"  queued {job.entry_id}  ({job.group}/{job.s3_key.split('/')[-1]})")
            except Exception as exc:
                group, obj = futs[fut]
                print(f"  SUBMIT ERROR [{group}] {obj['Key']}: {exc}")

    if args.no_wait:
        print(f"\n{len(jobs)} jobs submitted. Run without --no-wait to poll completion.")
        return

    print(f"\nPolling {len(jobs)} jobs for completion (timeout 30 min each)...")
    done = [0]

    def poll_and_report(job: Job) -> None:
        poll_job(args.api, job)
        done[0] += 1
        icon = "✓" if job.final_status == "indexed" else "✗"
        elapsed = job.finished_at - job.submitted_at
        print(f"  {icon} [{done[0]:>3}/{len(jobs)}] {job.group}/{job.s3_key.split('/')[-1]}"
              f"  {job.final_status}  {elapsed:.0f}s  chunks={job.chunk_count}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        list(ex.map(poll_and_report, jobs))

    print_summary(jobs)


if __name__ == "__main__":
    main()
