# vault-indexer

Async document indexing microservice for the Solo Vault backend.  
Accepts a file reference (S3/MinIO key), runs it through a multi-stage pipeline, and stores searchable vector embeddings in PostgreSQL + pgvector.

---

## Architecture

```
POST /index  (FastAPI)
  ├─ confidence < 0.6 → force kind = UNSORTED
  └─ enqueue Celery job via Redis
       └─ Celery worker
            ├─ validate   (MIME, size, S3 existence)
            ├─ download   (rate-limited S3/MinIO → /tmp; SHA-256 file-hash cache)
            ├─ parse      (docling PDF/DOCX · pytesseract images · raw text/code/CSV/YAML)
            ├─ chunk      (kind-aware: doc / code / data / single-image)
            ├─ embed      (BGE-M3 local model, chunk-level Redis cache)
            └─ store      (pgvector batch upsert + metadata)

Progress events → Redis pub/sub → WebSocket /ws/{entry_id}
```

### Services

| Service    | Image                  | Port        | Role                        |
|------------|------------------------|-------------|-----------------------------|
| `app`      | local build            | 8000        | FastAPI API + WebSocket     |
| `worker`   | local build            | —           | Celery worker (concurrency=2) |
| `postgres` | pgvector/pgvector:pg16 | 5432        | Entries + vector chunks     |
| `redis`    | redis:7-alpine         | 6379        | Broker · cache · pub/sub    |
| `minio`    | minio/minio            | 9000 / 9001 | S3-compatible object store  |

### Embedding

Uses **BAAI/bge-m3** (1024-dim dense vectors) running locally on CPU.  
Chunk embeddings are cached in Redis (30-day TTL); file hashes are cached to skip re-indexing unchanged files (7-day TTL).

### Parser selection

| Kind          | Parser                          |
|---------------|---------------------------------|
| DOCUMENT      | docling (PDF/DOCX) → pypdf fallback |
| CODE/SNIPPET  | raw text + language header      |
| IMAGE/DESIGN  | pytesseract OCR                 |
| DATA/CONFIG   | csv/json/yaml schema narrative  |
| WEB           | BeautifulSoup HTML → plain text |
| NOTE/KEYVALUE | raw UTF-8 text                  |
| ARCHIVE       | ZIP → recursive per-member      |
| UNSORTED      | best-effort UTF-8 decode        |

---

## Quick start

### Prerequisites

- Docker + Docker Compose
- ~6 GB free disk (BGE-M3 model + layers)

### Start the stack

```bash
cd services/indexer
make dev          # docker compose up --build (first run downloads BGE-M3 ~1.5 GB)
```

### Apply schema (first time only)

The FastAPI lifespan runs `schema.sql` automatically on startup, so this is
optional:

```bash
make migrate
```

### Smoke test

Upload a file via the MinIO console at <http://localhost:9001>  
(login `minioadmin` / `minioadmin`, bucket `vault-local`), then:

```bash
curl -s -X POST http://localhost:8000/index \
  -H "Content-Type: application/json" \
  -d '{
    "entry_id": "e1", "user_id": "u1", "project_id": null,
    "s3_key": "test/file.pdf", "bucket": "vault-local",
    "file_name": "file.pdf", "mime": "application/pdf",
    "kind": "document", "subkind": "pdf", "size_bytes": 40000,
    "title": "Smoke test", "tags": [], "classifier_confidence": 0.99,
    "pinned": false, "memory_type": ""
  }' | jq

# poll status
curl -s http://localhost:8000/jobs/e1 | jq '.status'

# watch worker logs
make logs
```

---

## API reference

### `POST /index`

Enqueue an indexing job.

| Field                  | Type            | Required | Description                                        |
|------------------------|-----------------|----------|----------------------------------------------------|
| `entry_id`             | string          | ✓        | Stable client ID (idempotency key)                 |
| `user_id`              | string          | ✓        | Tenant identifier — propagated to every chunk row  |
| `project_id`           | string \| null  |          | Optional project scope                             |
| `s3_key`               | string          | ✓        | Object key inside the bucket                       |
| `bucket`               | string          | ✓        | MinIO/S3 bucket name                               |
| `file_name`            | string          | ✓        | Original filename (used for extension checks)      |
| `mime`                 | string          | ✓        | MIME type                                          |
| `kind`                 | EntryKind       | ✓        | See EntryKind values below                         |
| `subkind`              | string          | ✓        | e.g. `"pdf"`, `"docx"`, `"python"`                |
| `size_bytes`           | int             | ✓        | File size (validated ≤ 50 MB)                      |
| `title`                | string          | ✓        |                                                    |
| `tags`                 | string[]        |          |                                                    |
| `classifier_confidence`| float           |          | `< 0.6` → kind forced to `UNSORTED` (default 1.0) |
| `pinned`               | bool            |          |                                                    |
| `memory_type`          | string          |          | e.g. `"episodic"`, `"semantic"`                   |

**Response** `200 OK`
```json
{ "job_id": "e1", "status": "queued" }
```

**EntryKind values:** `document` · `code` · `snippet` · `image` · `design` · `data` · `config` · `web` · `note` · `keyvalue` · `audio` · `archive` · `unsorted`

---

### `GET /jobs/{job_id}`

Poll indexing status.

**Response** `200 OK`
```json
{
  "job_id": "e1",
  "status": "indexed",
  "entry_id": "e1",
  "step": "indexed",
  "progress_pct": 100,
  "chunk_count": 12,
  "chunker_version": "1"
}
```

**Status values:** `pending` → `running` → `indexed` | `failed` | `deleted`

---

### `WS /ws/{entry_id}`

Real-time progress stream. Each message is a JSON object:

```json
{
  "type": "index_progress",
  "entry_id": "e1",
  "step": "embed",
  "progress_pct": 80,
  "status": "running",
  "message": "Embedding"
}
```

Progress milestones: `validate=10` · `download=25` · `parse=50` · `chunk=60` · `embed=80` · `store=90` · `done=100`

Connection closes automatically when `step` is `done` or `failed`.

---

## Configuration (`.env.local`)

| Variable                        | Default                 | Description                              |
|---------------------------------|-------------------------|------------------------------------------|
| `DB_HOST`                       | `postgres`              | Postgres host (Docker service name)      |
| `DB_PORT`                       | `5432`                  |                                          |
| `DB_NAME`                       | `vault`                 |                                          |
| `DB_USER`                       | `vault`                 |                                          |
| `DB_PASSWORD`                   | `vault`                 |                                          |
| `REDIS_URL`                     | `redis://redis:6379/0`  |                                          |
| `S3_ENDPOINT_URL`               | `http://minio:9000`     | `None` = real AWS                        |
| `S3_ACCESS_KEY`                 | `minioadmin`            |                                          |
| `S3_SECRET_KEY`                 | `minioadmin`            |                                          |
| `S3_BUCKET`                     | `vault-local`           |                                          |
| `S3_REGION`                     | `us-east-1`             |                                          |
| `EMBEDDING_MODEL`               | `BAAI/bge-m3`           |                                          |
| `EMBEDDING_DIM`                 | `1024`                  |                                          |
| `S3_RATE_LIMIT_RPS`             | `50`                    | Global S3 download rate limit            |
| `CLASSIFIER_CONFIDENCE_THRESHOLD` | `0.6`                 | Below this → UNSORTED                    |
| `PARSER_PREFER_DOCLING`         | `true`                  | Use docling for PDF/DOCX when available  |

---

## Testing

### Unit tests (offline — no docker-compose needed)

```bash
# Inside the container
docker compose run --rm app python -m pytest tests/test_hashing.py tests/test_chunker.py \
  tests/test_validate.py tests/test_parse.py tests/test_cache.py -v

# Or via Makefile shorthand
make test-unit
```

Test coverage per module:

| File                 | What it tests                                              |
|----------------------|------------------------------------------------------------|
| `test_hashing.py`    | SHA-256 file hash · chunk hash · determinism · edge cases  |
| `test_validate.py`   | MIME allowlist · code extension bypass · UNSORTED skip · size limit · S3 404 |
| `test_parse.py`      | `_normalize` · text/code/csv/json/yaml parsers · dispatcher |
| `test_chunker.py`    | All chunking strategies · overlap · determinism · dispatch  |
| `test_cache.py`      | Redis key patterns · TTLs · JSON roundtrip (mocked Redis)  |

### Integration tests (requires `make dev`)

```bash
make test
# or selectively:
docker compose run --rm app python -m pytest tests/test_routes.py tests/test_pipeline.py -v
```

| File                 | What it tests                                                      |
|----------------------|--------------------------------------------------------------------|
| `test_routes.py`     | POST /index · GET /jobs · 404 · confidence downgrade · field storage |
| `test_pipeline.py`   | Full pipeline for PDF · DOCX · PNG · EML/UNSORTED · idempotency · S3 deletion sync |

---

## Benchmarking

### Single-group benchmark with WebSocket stage timings

```bash
# Via Makefile (recommended — runs inside the app container)
make bench

# Or directly inside the container:
docker compose run --rm app python scripts/benchmark.py \
  --group all --limit 3 --out results_v1.json --api http://app:8000

# Compare two runs (e.g. before/after a parser change)
docker compose run --rm app python scripts/benchmark.py \
  --compare results_v1.json results_v2.json
```

Output includes per-file stage durations (parse · embed · store) and a summary table.

### Bulk indexing

```bash
# Via Makefile (recommended)
make bulk

# Or directly:
docker compose run --rm app python scripts/bulk_index.py \
  --group all --limit 10 --concurrency 2 --api http://app:8000

# Submit without waiting
docker compose run --rm app python scripts/bulk_index.py \
  --group dirty --limit 50 --no-wait --api http://app:8000
```

### Dataset groups (in `vault-test` bucket)

| Group        | Prefix         | Count | Notes                             |
|--------------|----------------|-------|-----------------------------------|
| `dirty`      | `dirty/`       | 101   | Scanned PDFs (docling OCR path)   |
| `pdf_native` | `pdf_native/`  | 101   | Native PDFs (fast text extract)   |
| `docx`       | `docx/`        | 101   | Word documents                    |
| `xlsx`       | `xlsx/`        | 101   | Spreadsheets → DATA kind          |
| `eml`        | `eml/`         | 303   | Email files → UNSORTED            |
| `png`        | `png/`         | 101   | Scanned page images               |

### Inspect results in DB

```bash
docker compose exec postgres psql -U vault -d vault -c "
SELECT kind, index_status, count(*), avg(chunk_count)::int avg_chunks
FROM vault_entries GROUP BY kind, index_status ORDER BY kind;"
```

---

## Makefile targets

| Target       | Description                                      |
|--------------|--------------------------------------------------|
| `make dev`   | Build + start all services                       |
| `make worker`| Start only the Celery worker                     |
| `make test`  | Run full test suite inside the `app` container   |
| `make migrate` | Apply `schema.sql` manually                    |
| `make logs`  | Tail `app` + `worker` logs                       |
| `make clean` | Stop containers and remove volumes               |

---

## Design notes

- **Idempotency** — `POST /index` with the same `entry_id` upserts the DB row and re-queues without error.
- **File-hash cache** — identical file content (same SHA-256) short-circuits after download; no re-parse/embed.
- **Chunker version** — `CHUNKER_VERSION = "1"` (in `config.py`). Bump to trigger targeted reindexing when chunking logic changes.
- **Deletion sync** — if the S3 object disappears between enqueue and validate, the entry is marked `deleted` (not `failed`) and never retried.
- **Multi-tenant isolation** — `user_id` is denormalized into every `vault_chunks` row so RAG queries can filter with `WHERE user_id = $1` without a join.
- **Rate limiting** — two-level token bucket: per-worker `LocalTokenBucket` + distributed `RedisGlobalTokenBucket` (Lua script), default 50 RPS.
