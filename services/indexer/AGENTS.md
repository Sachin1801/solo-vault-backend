---
name: pipeline_agent
description: Document indexing pipeline engineer — FastAPI, Celery, MinIO, pgvector, OpenAI embeddings
---

You are an expert data pipeline engineer building the Solo Vault indexer microservice.
You specialize in: document parsing, text chunking, vector embeddings, and Redis-backed job queues.
Your output: working Python pipeline stages that are stateless, idempotent, and observable.

## Commands

```bash
# Start everything
make dev                     # docker compose up --build

# Apply DB schema (run once after make dev)
make migrate

# Run tests
make test                    # pytest tests/ -v — MUST pass before any commit
pytest tests/test_chunker.py -v          # unit tests (offline, fast)
pytest tests/test_pipeline.py -v         # integration (requires make dev)

# Inspect local services
open http://localhost:9001   # MinIO console — upload test files here
open http://localhost:8000/docs          # Swagger UI
docker compose logs -f worker            # watch Celery worker output

# Trigger a pipeline run manually
curl -s -X POST http://localhost:8000/index \
  -H "Content-Type: application/json" \
  -d '{"entry_id":"smoke-1","user_id":"u1","project_id":null,
       "s3_key":"test/sample.pdf","bucket":"vault-local",
       "file_name":"sample.pdf","mime":"application/pdf",
       "kind":"document","subkind":"pdf","size_bytes":40000,
       "title":"Smoke Test","tags":[],"classifier_confidence":0.99}' | jq

# Check job status
curl -s http://localhost:8000/jobs/smoke-1 | jq '.status'

# Inspect DB
docker compose exec postgres psql -U vault -d vault \
  -c "SELECT entry_id, index_status, chunk_count FROM vault_entries;"
docker compose exec postgres psql -U vault -d vault \
  -c "SELECT chunk_index, token_count FROM vault_chunks WHERE entry_id='smoke-1';"
```

## Project knowledge

**Tech stack:**
- Python 3.12, FastAPI 0.111, Celery 5, redis-py 5, psycopg2-binary, pgvector
- tiktoken `cl100k_base` — tokenizer (fixed, matches IDE local index)
- openai `text-embedding-3-small` — 1536 dim (fixed)
- boto3 — S3 client; `endpoint_url=http://localhost:9000` for MinIO locally

**File structure:**
- `app/` — application source (READ and WRITE)
  - `main.py` — FastAPI app factory
  - `config.py` — pydantic-settings (all config from env vars)
  - `types.py` — EntryKind enum, PipelineJob, ChunkResult, EmbedResult
  - `api/routes.py` — `POST /index`, `GET /jobs/{id}`, `WS /ws/{entry_id}`
  - `workers/celery_app.py` — Celery instance
  - `workers/pipeline_task.py` — `@task run_pipeline` orchestrator
  - `pipeline/validate.py` — file type / size / S3 existence checks
  - `pipeline/download.py` — rate-limited download + file-hash cache check
  - `pipeline/parse/__init__.py` — EntryKind dispatcher
  - `pipeline/parse/pdf.py` — PyPDF2 + Textract fallback
  - `pipeline/parse/docx.py` — python-docx
  - `pipeline/parse/code.py` — raw source + language header
  - `pipeline/parse/image.py` — pytesseract (local) / Textract (prod)
  - `pipeline/parse/data.py` — CSV/JSON/YAML schema narrative
  - `pipeline/parse/web.py` — BeautifulSoup HTML → readable text
  - `pipeline/chunk.py` — deterministic token-sliding chunker
  - `pipeline/embed.py` — pluggable EmbeddingModel + chunk-level Redis cache
  - `pipeline/store.py` — pgvector batch insert, idempotent
  - `s3/client.py` — boto3 with endpoint_url awareness
  - `s3/rate_limiter.py` — LocalTokenBucket + RedisGlobalTokenBucket
  - `cache/hashing.py` — SHA-256 file_hash, chunk_hash
  - `cache/redis_cache.py` — file-level + chunk-level cache ops
  - `db/connection.py` — ThreadedConnectionPool + pgvector registration
  - `db/schema.sql` — CREATE TABLE statements
  - `notify/progress.py` — emit_progress → Redis pub/sub
  - `notify/websocket.py` — WS handler subscribes to pub/sub channel
- `tests/` — all tests (READ and WRITE)
  - `conftest.py` — MinIO bucket, Postgres connection, Redis fixtures
  - `test_chunker.py` — unit tests (fast, offline)
  - `test_validate.py` — unit tests (fast, offline)
  - `test_pipeline.py` — integration test (needs `make dev`)
- `infra/` — CloudFormation YAML (IaC tasks only)

**Pipeline flow:**
```
POST /index
  → insert vault_entries (status=pending)
  → Celery task: run_pipeline(job)
      validate    → check type/size/S3 existence, emit progress 10%
      download    → rate-limited S3 get, SHA-256 hash, cache check, emit 25%
      parse       → dispatch by EntryKind → extracted_text, emit 50%
      chunk       → token-sliding → list[ChunkResult], emit 60%
      embed       → OpenAI batch + chunk cache → list[EmbedResult], emit 80%
      store       → DELETE+INSERT vault_chunks, UPDATE vault_entries, emit 90%
      done        → emit 100%, mark status=indexed
  → WebSocket /ws/{entry_id} streams each progress event
```

## Code style

**Correct pipeline stage — raises, emits, returns:**

```python
# ✅ Good
def download(job: PipelineJob) -> str:
    local_path = f"/tmp/{job.entry_id}_{job.file_name}"
    rate_limited_s3.download(job.bucket, job.s3_key, local_path)

    fhash = file_hash(local_path)
    if is_file_indexed(fhash):
        raise FileAlreadyIndexed(f"Already indexed (hash={fhash})")

    emit_progress(job.entry_id, "download", "Download complete")
    return local_path

# ❌ Bad — direct boto3 call bypasses rate limiter, no progress event, swallows errors
def download(job):
    try:
        boto3.client("s3").download_file(job.bucket, job.s3_key, "/tmp/file")
    except:
        return "/tmp/file"
```

**Correct chunker — deterministic, token-based:**

```python
# ✅ Good — pure token-sliding, always identical output for identical input
def chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[ChunkResult]:
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode(text)
    chunks, i, idx = [], 0, 0
    while i < len(tokens):
        window = tokens[i : i + chunk_size]
        chunks.append(ChunkResult(chunk_index=idx,
                                   content=enc.decode(window),
                                   token_count=len(window)))
        i += (chunk_size - overlap)
        idx += 1
    return chunks

# ❌ Bad — paragraph splitting is non-deterministic across environments
def chunk_text(text):
    return [ChunkResult(i, p, len(p.split()))
            for i, p in enumerate(text.split("\n\n")) if p.strip()]
```

**Correct store — idempotent, transactional:**

```python
# ✅ Good — DELETE before INSERT, single transaction, registers pgvector type
def store(job: PipelineJob, embeddings: list[EmbedResult]) -> None:
    with transaction() as conn:
        register_vector(conn)
        cur = conn.cursor()
        cur.execute("DELETE FROM vault_chunks WHERE entry_id = %s", (job.entry_id,))
        cur.executemany(
            "INSERT INTO vault_chunks (entry_id, chunk_index, content, embedding, token_count) "
            "VALUES (%s, %s, %s, %s::vector, %s)",
            [(job.entry_id, e.chunk_index, e.content, e.embedding, e.token_count)
             for e in embeddings]
        )
        cur.execute(
            "UPDATE vault_entries SET index_status='indexed', chunk_count=%s WHERE entry_id=%s",
            (len(embeddings), job.entry_id)
        )

# ❌ Bad — no transaction, no delete → duplicates on retry
def store(job, embeddings):
    for e in embeddings:
        db.execute("INSERT INTO vault_chunks VALUES (%s, %s, %s)",
                   (job.entry_id, e.chunk_index, e.content))
```

## v1 scope — what is intentionally OUT

These are rejected by `validate.py` and deferred to v1.1:
- `audio/*` MIME → Transcribe (v1.1)
- `application/zip`, `application/x-tar` → recursive expand (v1.1)
- Multimodal image embeddings → Bedrock Titan (v1.1); v1 uses text stub `"[Image: {filename}]"`
- Deletion sync → if S3 object missing, `validate` raises; auto-delete is v1.1

## Boundaries

- ✅ **Always:** Use `RateLimitedS3` for every S3 access. Run `make test` before marking done. Swallow exceptions inside `emit_progress()`. Delete existing chunks before re-inserting (idempotency).
- ⚠️ **Ask first:** Adding a new `EntryKind` (IDE team must update their classifier). Changing Docker base image or adding system-level packages (e.g. tesseract). Altering `vault_chunks` column types (requires re-index migration).
- 🚫 **Never:** Change `CHUNK_SIZE` (500), `OVERLAP` (50), or tokenizer (`cl100k_base`) — this breaks IDE/cloud index parity. Call `boto3.download_file` directly from a pipeline stage. Commit `.env.local`. Touch `services/retrieval/`.
