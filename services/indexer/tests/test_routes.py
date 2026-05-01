"""Integration tests for FastAPI routes — requires docker-compose stack running."""

import os
import socket
import time
import uuid

import httpx
import pytest

API_BASE_URL = os.getenv("TEST_API_BASE_URL", "http://app:8000")


@pytest.fixture(scope="module", autouse=True)
def require_api_running():
    host_port = API_BASE_URL.replace("http://", "").replace("https://", "").split("/")[0]
    host, _, port = host_port.partition(":")
    port_num = int(port) if port else 80
    try:
        with socket.create_connection((host, port_num), timeout=2):
            return
    except OSError:
        pytest.skip(f"API not reachable at {API_BASE_URL} — run: make dev")


def _unique_entry_id() -> str:
    return f"route-test-{uuid.uuid4().hex[:8]}"


_BASE_PAYLOAD = {
    "user_id": "route-test-user",
    "project_id": None,
    "s3_key": "pdf_native/clean_0001011452_00_000001.pdf",
    "bucket": "vault-test",
    "file_name": "clean.pdf",
    "mime": "application/pdf",
    "kind": "document",
    "subkind": "pdf",
    "size_bytes": 4832,
    "title": "Route Test",
    "tags": ["route-test"],
    "classifier_confidence": 0.99,
    "pinned": False,
    "memory_type": "",
}


# ── POST /index ───────────────────────────────────────────────────────────────


def test_index_returns_queued():
    entry_id = _unique_entry_id()
    resp = httpx.post(f"{API_BASE_URL}/index",
                      json={**_BASE_PAYLOAD, "entry_id": entry_id}, timeout=10)
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == entry_id
    assert body["status"] == "queued"


def test_index_invalid_kind_returns_400():
    resp = httpx.post(f"{API_BASE_URL}/index",
                      json={**_BASE_PAYLOAD, "entry_id": _unique_entry_id(),
                            "kind": "not_a_kind"}, timeout=10)
    assert resp.status_code == 400


def test_index_confidence_below_threshold_stored_as_unsorted(db_conn):
    entry_id = _unique_entry_id()
    resp = httpx.post(f"{API_BASE_URL}/index",
                      json={**_BASE_PAYLOAD, "entry_id": entry_id,
                            "classifier_confidence": 0.1}, timeout=10)
    assert resp.status_code == 200

    cur = db_conn.cursor()
    cur.execute("SELECT kind FROM vault_entries WHERE entry_id = %s", (entry_id,))
    row = cur.fetchone()
    assert row is not None
    assert row[0] == "unsorted"


def test_index_idempotent_requeue(db_conn):
    """POSTing the same entry_id twice updates the row and re-queues without error."""
    entry_id = _unique_entry_id()
    payload = {**_BASE_PAYLOAD, "entry_id": entry_id}
    r1 = httpx.post(f"{API_BASE_URL}/index", json=payload, timeout=10)
    r2 = httpx.post(f"{API_BASE_URL}/index", json=payload, timeout=10)
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_index_stores_tags_and_metadata(db_conn):
    entry_id = _unique_entry_id()
    httpx.post(f"{API_BASE_URL}/index",
               json={**_BASE_PAYLOAD, "entry_id": entry_id,
                     "tags": ["alpha", "beta"]}, timeout=10)
    cur = db_conn.cursor()
    cur.execute("SELECT tags FROM vault_entries WHERE entry_id = %s", (entry_id,))
    row = cur.fetchone()
    assert row is not None
    assert set(row[0]) == {"alpha", "beta"}


def test_index_stores_pinned_flag(db_conn):
    entry_id = _unique_entry_id()
    httpx.post(f"{API_BASE_URL}/index",
               json={**_BASE_PAYLOAD, "entry_id": entry_id, "pinned": True}, timeout=10)
    cur = db_conn.cursor()
    cur.execute("SELECT pinned FROM vault_entries WHERE entry_id = %s", (entry_id,))
    assert cur.fetchone()[0] is True


# ── GET /jobs/{job_id} ────────────────────────────────────────────────────────


def test_jobs_unknown_id_returns_404():
    resp = httpx.get(f"{API_BASE_URL}/jobs/definitely-does-not-exist", timeout=5)
    assert resp.status_code == 404


def test_jobs_returns_expected_fields():
    entry_id = _unique_entry_id()
    httpx.post(f"{API_BASE_URL}/index",
               json={**_BASE_PAYLOAD, "entry_id": entry_id}, timeout=10)

    resp = httpx.get(f"{API_BASE_URL}/jobs/{entry_id}", timeout=5)
    assert resp.status_code == 200
    body = resp.json()
    for field in ("job_id", "status", "entry_id", "step", "progress_pct", "chunk_count"):
        assert field in body, f"Missing field: {field}"


def test_jobs_status_is_valid_value():
    entry_id = _unique_entry_id()
    httpx.post(f"{API_BASE_URL}/index",
               json={**_BASE_PAYLOAD, "entry_id": entry_id}, timeout=10)

    resp = httpx.get(f"{API_BASE_URL}/jobs/{entry_id}", timeout=5)
    valid = {"pending", "running", "indexed", "failed", "deleted"}
    assert resp.json()["status"] in valid


def test_jobs_progress_pct_in_range():
    entry_id = _unique_entry_id()
    httpx.post(f"{API_BASE_URL}/index",
               json={**_BASE_PAYLOAD, "entry_id": entry_id}, timeout=10)

    resp = httpx.get(f"{API_BASE_URL}/jobs/{entry_id}", timeout=5)
    pct = resp.json()["progress_pct"]
    assert 0 <= pct <= 100


def test_jobs_chunk_count_non_negative():
    entry_id = _unique_entry_id()
    httpx.post(f"{API_BASE_URL}/index",
               json={**_BASE_PAYLOAD, "entry_id": entry_id}, timeout=10)
    resp = httpx.get(f"{API_BASE_URL}/jobs/{entry_id}", timeout=5)
    assert resp.json()["chunk_count"] >= 0


# ── GET /docs (sanity) ────────────────────────────────────────────────────────


def test_openapi_docs_reachable():
    resp = httpx.get(f"{API_BASE_URL}/docs", timeout=5)
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower() or "openapi" in resp.text.lower()
