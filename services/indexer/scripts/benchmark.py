#!/usr/bin/env python3
"""
Pipeline benchmarker — measures per-stage latency by subscribing to
the WebSocket progress stream for each job.

Usage:
    python scripts/benchmark.py [--group dirty|pdf_native|docx|xlsx|eml|png|all]
                                 [--limit N]       # files per group (default 5)
                                 [--api http://localhost:8000]
                                 [--user bench-user]
                                 [--out results.json]

Output:
    - Live stage timings per file
    - Summary table: group × stage (avg seconds)
    - JSON results file for diffing runs
"""

import argparse
import json
import statistics
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

import boto3
import httpx
import websocket  # websocket-client

MINIO_ENDPOINT = "http://localhost:9000"
MINIO_ACCESS   = "minioadmin"
MINIO_SECRET   = "minioadmin"
BUCKET         = "vault-test"

STAGES = ["validate", "download", "parse", "chunk", "embed", "store", "done"]

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


@dataclass
class BenchResult:
    entry_id: str
    group: str
    file_name: str
    size_bytes: int
    final_status: str = "pending"
    chunk_count: int = 0
    # stage_name → elapsed seconds since job submitted
    stage_times: dict[str, float] = field(default_factory=dict)
    # stage_name → wall-clock duration (time between consecutive stage events)
    stage_durations: dict[str, float] = field(default_factory=dict)
    total_s: float = 0.0
    error: str = ""


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


def run_benchmark_job(api: str, user: str, group: str, obj: dict) -> BenchResult:
    cfg = GROUP_CONFIG[group]
    key = obj["Key"]
    entry_id = f"bench-{uuid.uuid4().hex[:10]}"
    file_name = key.split("/")[-1]

    result = BenchResult(
        entry_id=entry_id,
        group=group,
        file_name=file_name,
        size_bytes=obj["Size"],
    )

    # Submit the job
    payload = {
        "entry_id": entry_id,
        "user_id": user,
        "project_id": None,
        "s3_key": key,
        "bucket": BUCKET,
        "file_name": file_name,
        "mime": cfg["mime"],
        "kind": cfg["kind"],
        "subkind": cfg["subkind"],
        "size_bytes": obj["Size"],
        "title": file_name,
        "tags": ["benchmark", group],
        "classifier_confidence": 0.95,
        "pinned": False,
        "memory_type": "",
    }
    t_submit = time.time()
    resp = httpx.post(f"{api}/index", json=payload, timeout=10)
    resp.raise_for_status()

    # Subscribe to WebSocket for stage timings
    ws_url = api.replace("http://", "ws://") + f"/ws/{entry_id}"
    events_done = threading.Event()
    prev_stage_time = [t_submit]

    def on_message(ws, message):
        t_now = time.time()
        try:
            data = json.loads(message)
        except Exception:
            return
        step = data.get("step", "")
        elapsed = t_now - t_submit
        result.stage_times[step] = elapsed
        # Duration = time since previous stage event
        result.stage_durations[step] = t_now - prev_stage_time[0]
        prev_stage_time[0] = t_now

        if data.get("progress_pct") in (100, 0) and step in ("done", "failed"):
            result.final_status = data.get("status", step)
            result.total_s = elapsed
            events_done.set()
            ws.close()

    def on_error(ws, error):
        result.error = str(error)
        events_done.set()

    def on_open(ws):
        pass

    ws = websocket.WebSocketApp(
        ws_url,
        on_message=on_message,
        on_error=on_error,
        on_open=on_open,
    )
    t = threading.Thread(target=ws.run_forever, daemon=True)
    t.start()

    # Wait up to 30 minutes
    if not events_done.wait(timeout=1800):
        result.final_status = "timeout"
        result.total_s = time.time() - t_submit

    # Get chunk count from API
    try:
        r = httpx.get(f"{api}/jobs/{entry_id}", timeout=5)
        if r.status_code == 200:
            result.chunk_count = r.json().get("chunk_count", 0)
    except Exception:
        pass

    return result


def print_report(results: list[BenchResult]) -> None:
    by_group: dict[str, list[BenchResult]] = defaultdict(list)
    for r in results:
        by_group[r.group].append(r)

    print("\n" + "=" * 90)
    print(f"{'BENCHMARK RESULTS':^90}")
    print("=" * 90)

    # Per-stage averages by group
    stage_cols = ["parse", "embed", "store", "done"]
    col_w = 10
    header = f"{'Group':<12} {'N':>4} {'OK':>4} {'Chunks':>7} {'Total(avg)':>11}"
    for s in stage_cols:
        header += f" {s[:col_w]:>{col_w}}"
    print(header)
    print("-" * 90)

    for group, rs in sorted(by_group.items()):
        ok = [r for r in rs if r.final_status == "indexed"]
        n_ok = len(ok)
        avg_chunks = statistics.mean(r.chunk_count for r in ok) if ok else 0
        avg_total = statistics.mean(r.total_s for r in ok) if ok else 0
        row = f"{group:<12} {len(rs):>4} {n_ok:>4} {avg_chunks:>7.1f} {avg_total:>10.1f}s"
        for s in stage_cols:
            durs = [r.stage_durations[s] for r in ok if s in r.stage_durations]
            avg = statistics.mean(durs) if durs else 0
            row += f" {avg:>{col_w}.1f}s"
        print(row)

    print("=" * 90)

    # Per-file detail
    print(f"\n{'PER-FILE DETAIL':^90}")
    print("-" * 90)
    for r in results:
        stages_str = "  ".join(
            f"{s}={r.stage_durations.get(s, 0):.1f}s" for s in STAGES if s in r.stage_durations
        )
        icon = "✓" if r.final_status == "indexed" else "✗"
        print(f"  {icon} {r.group}/{r.file_name} ({r.size_bytes//1024}KB)"
              f"  chunks={r.chunk_count}  total={r.total_s:.1f}s")
        if stages_str:
            print(f"      {stages_str}")
        if r.error:
            print(f"      ERROR: {r.error}")

    print("=" * 90)


def save_json(results: list[BenchResult], path: str) -> None:
    data = {
        "run_at": datetime.now().isoformat(),
        "results": [
            {
                "entry_id": r.entry_id,
                "group": r.group,
                "file_name": r.file_name,
                "size_bytes": r.size_bytes,
                "final_status": r.final_status,
                "chunk_count": r.chunk_count,
                "total_s": r.total_s,
                "stage_durations": r.stage_durations,
            }
            for r in results
        ],
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\nResults saved to {path}")


def compare_runs(path_a: str, path_b: str) -> None:
    with open(path_a) as f:
        a = json.load(f)
    with open(path_b) as f:
        b = json.load(f)

    print(f"\nCOMPARISON: {path_a}  vs  {path_b}")
    print(f"Run A: {a['run_at']}  ({len(a['results'])} files)")
    print(f"Run B: {b['run_at']}  ({len(b['results'])} files)")
    print("-" * 60)

    def avg_by_group(results, key):
        by_group = defaultdict(list)
        for r in results:
            if r["final_status"] == "indexed" and key in r.get("stage_durations", {}):
                by_group[r["group"]].append(r["stage_durations"][key])
        return {g: statistics.mean(v) for g, v in by_group.items()}

    for stage in ["parse", "embed", "store"]:
        a_avgs = avg_by_group(a["results"], stage)
        b_avgs = avg_by_group(b["results"], stage)
        all_groups = sorted(set(a_avgs) | set(b_avgs))
        print(f"\n  Stage: {stage}")
        for g in all_groups:
            av = a_avgs.get(g)
            bv = b_avgs.get(g)
            if av and bv:
                delta = ((bv - av) / av) * 100
                arrow = "▲" if delta > 0 else "▼"
                print(f"    {g:<14} A={av:.1f}s  B={bv:.1f}s  {arrow}{abs(delta):.1f}%")
            elif av:
                print(f"    {g:<14} A={av:.1f}s  B=n/a")
            elif bv:
                print(f"    {g:<14} A=n/a     B={bv:.1f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark the indexer pipeline")
    parser.add_argument("--group", default="all")
    parser.add_argument("--limit", type=int, default=5,
                        help="Files per group (default 5)")
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--user", default="bench-user")
    parser.add_argument("--out", default="",
                        help="Save results to JSON (e.g. results_v1.json)")
    parser.add_argument("--compare", nargs=2, metavar=("A.json", "B.json"),
                        help="Compare two saved result files instead of running")
    args = parser.parse_args()

    if args.compare:
        compare_runs(args.compare[0], args.compare[1])
        return

    try:
        import websocket  # noqa: F401
    except ImportError:
        print("ERROR: websocket-client not installed. Run: pip install websocket-client")
        raise SystemExit(1)

    s3 = boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        aws_access_key_id=MINIO_ACCESS,
        aws_secret_access_key=MINIO_SECRET,
        region_name="us-east-1",
    )

    groups = list(GROUP_CONFIG.keys()) if args.group == "all" else [args.group]

    all_objects: list[tuple[str, dict]] = []
    for group in groups:
        objs = list_objects(s3, group, args.limit)
        print(f"  {group}: {len(objs)} files")
        all_objects.extend((group, obj) for obj in objs)

    print(f"\nRunning benchmark on {len(all_objects)} files (sequential per group)...")
    results: list[BenchResult] = []

    for group, obj in all_objects:
        fname = obj["Key"].split("/")[-1]
        print(f"\n  → {group}/{fname} ({obj['Size']//1024}KB)", flush=True)
        r = run_benchmark_job(args.api, args.user, group, obj)
        icon = "✓" if r.final_status == "indexed" else "✗"
        print(f"    {icon} {r.final_status}  total={r.total_s:.1f}s  chunks={r.chunk_count}")
        results.append(r)

    print_report(results)

    if args.out:
        save_json(results, args.out)


if __name__ == "__main__":
    main()
