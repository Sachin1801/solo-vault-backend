"""End-to-end pipeline integration tests — requires docker-compose stack running."""

import os
import socket
import time
import uuid
from io import BytesIO
from pathlib import Path

import httpx
import pytest

API_BASE_URL = os.getenv("TEST_API_BASE_URL", "http://app:8000")
POLL_INTERVAL = 5   # seconds between status polls
POLL_TIMEOUT  = 600 # seconds max wait per job (docling on CPU is slow)


@pytest.fixture(scope="module", autouse=True)
def require_api_running():
    host_port = API_BASE_URL.replace("http://", "").replace("https://", "").split("/")[0]
    host, _, port = host_port.partition(":")
    port_num = int(port) if port else 80
    try:
        with socket.create_connection((host, port_num), timeout=2):
            return
    except OSError:
        pytest.skip(f"Integration API not reachable at {API_BASE_URL}")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _uid() -> str:
    return f"pipe-{uuid.uuid4().hex[:8]}"


def _wait_for(entry_id: str, timeout: int = POLL_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{API_BASE_URL}/jobs/{entry_id}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data["status"] in ("indexed", "failed", "deleted"):
                return data
        time.sleep(POLL_INTERVAL)
    return httpx.get(f"{API_BASE_URL}/jobs/{entry_id}", timeout=10).json()


def _submit(entry_id: str, **overrides) -> httpx.Response:
    payload = {
        "entry_id": entry_id,
        "user_id": "test-user",
        "project_id": None,
        "s3_key": "pdf_native/clean_0001011452_00_000001.pdf",
        "bucket": "vault-test",
        "file_name": "clean.pdf",
        "mime": "application/pdf",
        "kind": "document",
        "subkind": "pdf",
        "size_bytes": 4832,
        "title": "Pipeline Test",
        "tags": [],
        "classifier_confidence": 0.99,
        "pinned": False,
        "memory_type": "",
    }
    payload.update(overrides)
    return httpx.post(f"{API_BASE_URL}/index", json=payload, timeout=15)


def _assert_chunks_in_db(db_conn, entry_id: str) -> list:
    cur = db_conn.cursor()
    cur.execute(
        "SELECT chunk_index, token_count, content FROM vault_chunks "
        "WHERE entry_id = %s ORDER BY chunk_index",
        (entry_id,),
    )
    return cur.fetchall()


# ── Native PDF (small, fast) ──────────────────────────────────────────────────


def test_full_pipeline_native_pdf(db_conn):
    entry_id = _uid()
    resp = _submit(entry_id)
    assert resp.status_code == 200

    result = _wait_for(entry_id)
    assert result["status"] == "indexed", f"status={result['status']}"

    rows = _assert_chunks_in_db(db_conn, entry_id)
    assert len(rows) > 0
    assert rows[0][0] == 0  # chunk_index starts at 0
    assert all(r[1] > 0 for r in rows)  # all chunks have tokens


def test_native_pdf_metadata_stored(db_conn):
    entry_id = _uid()
    _submit(entry_id)
    _wait_for(entry_id)

    cur = db_conn.cursor()
    cur.execute(
        "SELECT chunker_version, embedding_model, file_hash, kind "
        "FROM vault_entries WHERE entry_id = %s",
        (entry_id,),
    )
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "1"               # chunker_version
    assert row[1] == "BAAI/bge-m3"    # embedding_model
    assert row[2]                      # file_hash set
    assert row[3] == "document"        # kind preserved


def test_native_pdf_user_id_on_chunks(db_conn):
    entry_id = _uid()
    _submit(entry_id, user_id="tenant-42")
    _wait_for(entry_id)

    cur = db_conn.cursor()
    cur.execute(
        "SELECT DISTINCT user_id FROM vault_chunks WHERE entry_id = %s",
        (entry_id,),
    )
    assert cur.fetchone()[0] == "tenant-42"


# ── DOCX ─────────────────────────────────────────────────────────────────────


def test_full_pipeline_docx(db_conn):
    entry_id = _uid()
    resp = _submit(
        entry_id,
        s3_key="docx/doc_0001011452_00_000001.docx",
        file_name="doc_0001011452_00_000001.docx",
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        subkind="docx",
        size_bytes=38485,
    )
    assert resp.status_code == 200

    result = _wait_for(entry_id)
    assert result["status"] == "indexed"
    assert result["chunk_count"] > 0


# ── Image / PNG ───────────────────────────────────────────────────────────────


def test_full_pipeline_png(db_conn):
    entry_id = _uid()
    resp = _submit(
        entry_id,
        s3_key="png/img_0001011452_00_000001_p1.png",
        file_name="img_0001011452_00_000001_p1.png",
        mime="image/png",
        kind="image",
        subkind="png",
        size_bytes=201561,
    )
    assert resp.status_code == 200

    result = _wait_for(entry_id)
    assert result["status"] == "indexed"
    # Images are single-chunk
    rows = _assert_chunks_in_db(db_conn, entry_id)
    assert len(rows) == 1


# ── EML / UNSORTED ────────────────────────────────────────────────────────────


def test_full_pipeline_eml_becomes_unsorted(db_conn):
    """EML is not in ALLOWED_MIMES — pipeline should index it as UNSORTED."""
    entry_id = _uid()
    resp = _submit(
        entry_id,
        s3_key="eml/email_0001011452_00_000001_msg1.eml",
        file_name="email_0001011452_00_000001_msg1.eml",
        mime="message/rfc822",
        kind="note",      # caller-supplied kind; validate will raise INVALID_TYPE
        subkind="eml",
        size_bytes=1374,
        classifier_confidence=0.1,  # force UNSORTED before validate
    )
    assert resp.status_code == 200
    result = _wait_for(entry_id)
    # UNSORTED bypasses mime check → should index successfully
    assert result["status"] == "indexed"


# ── Confidence threshold ──────────────────────────────────────────────────────


def test_confidence_below_threshold_becomes_unsorted(s3_client, test_bucket, sample_pdf, db_conn):
    s3_client.upload_file(str(sample_pdf), test_bucket, "test/low_conf.pdf")
    entry_id = _uid()
    resp = _submit(
        entry_id,
        s3_key="test/low_conf.pdf",
        bucket=test_bucket,
        file_name="low_conf.pdf",
        classifier_confidence=0.4,
    )
    assert resp.status_code == 200

    cur = db_conn.cursor()
    cur.execute("SELECT kind FROM vault_entries WHERE entry_id = %s", (entry_id,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "unsorted"


# ── File-hash cache: idempotency ──────────────────────────────────────────────


def test_same_file_twice_second_is_already_indexed(db_conn):
    """Submitting the identical file a second time hits the file-hash cache
    and returns status=indexed without re-running the full pipeline."""
    entry_id_1 = _uid()
    entry_id_2 = _uid()

    _submit(entry_id_1)
    r1 = _wait_for(entry_id_1)
    assert r1["status"] == "indexed"

    # Submit the exact same S3 key under a new entry_id
    _submit(entry_id_2)
    r2 = _wait_for(entry_id_2, timeout=60)  # cache hit is near-instant
    assert r2["status"] == "indexed"


# ── Chunker version stored ────────────────────────────────────────────────────


def test_full_pipeline_stores_chunker_version(db_conn):
    entry_id = _uid()
    _submit(entry_id)
    _wait_for(entry_id)

    cur = db_conn.cursor()
    cur.execute(
        "SELECT chunker_version FROM vault_entries WHERE entry_id = %s", (entry_id,)
    )
    assert cur.fetchone()[0] == "1"


# ── S3 deletion sync ──────────────────────────────────────────────────────────


def test_missing_s3_object_marks_entry_deleted(s3_client, test_bucket, db_conn):
    """If S3 object is removed between enqueue and validate, entry is marked deleted."""
    entry_id = _uid()
    ghost_key = f"test/ghost-{entry_id}.pdf"

    # Upload then immediately delete so the file is gone when the worker runs
    from io import BytesIO
    s3_client.put_object(Bucket=test_bucket, Key=ghost_key, Body=b"%PDF-1.4")
    s3_client.delete_object(Bucket=test_bucket, Key=ghost_key)

    resp = _submit(
        entry_id,
        s3_key=ghost_key,
        bucket=test_bucket,
        file_name="ghost.pdf",
        size_bytes=8,
    )
    assert resp.status_code == 200

    result = _wait_for(entry_id, timeout=60)
    assert result["status"] == "deleted"


# ── Original integration tests (preserved) ───────────────────────────────────


def test_full_pipeline_pdf(s3_client, test_bucket, db_conn, sample_pdf):
    s3_client.upload_file(str(sample_pdf), test_bucket, "test/sample.pdf")

    resp = httpx.post(
        f"{API_BASE_URL}/index",
        json={
            "entry_id": "test-entry-001",
            "user_id": "user-1",
            "project_id": None,
            "s3_key": "test/sample.pdf",
            "bucket": test_bucket,
            "file_name": "sample.pdf",
            "mime": "application/pdf",
            "kind": "document",
            "subkind": "pdf",
            "size_bytes": sample_pdf.stat().st_size,
            "title": "Test PDF",
            "tags": [],
            "classifier_confidence": 0.99,
            "pinned": False,
            "memory_type": "",
        },
        timeout=30,
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status = "pending"
    for _ in range(POLL_TIMEOUT // POLL_INTERVAL):
        status_resp = httpx.get(f"{API_BASE_URL}/jobs/{job_id}", timeout=10)
        status = status_resp.json()["status"]
        if status in ("indexed", "failed", "deleted"):
            break
        time.sleep(POLL_INTERVAL)

    assert status == "indexed"

    cur = db_conn.cursor()
    cur.execute(
        "SELECT chunk_index, token_count FROM vault_chunks "
        "WHERE entry_id = %s ORDER BY chunk_index",
        ("test-entry-001",),
    )
    rows = cur.fetchall()
    assert len(rows) > 0
    assert rows[0][0] == 0

    cur.execute(
        "SELECT chunker_version, embedding_model FROM vault_entries WHERE entry_id = %s",
        ("test-entry-001",),
    )
    meta = cur.fetchone()
    assert meta[0] == "1"

    cur.execute(
        "SELECT DISTINCT user_id FROM vault_chunks WHERE entry_id = %s",
        ("test-entry-001",),
    )
    assert cur.fetchone()[0] == "user-1"
