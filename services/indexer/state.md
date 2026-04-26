# Project State -- Solo Vault Indexer

**Snapshot date:** 2026-04-23
**Branch:** main (services/indexer/)
**Last commit:** fdc9fcb fix(indexer): read S3 endpoint from env in bench/bulk scripts

---

## Current Architecture

```
Docker Compose (local dev):
  +-- app       (FastAPI, port 8000)
  +-- worker    (Celery, concurrency=2)
  +-- postgres  (pgvector/pgvector:pg16, port 5432)
  +-- redis     (redis:7-alpine, port 6379)
  +-- minio     (MinIO S3, ports 9000/9001)
```

Pipeline: monolithic Celery task -> 6 sequential stages in one process.

---

## Codebase Map

```
services/indexer/
+-- app/
|   +-- api/routes.py          POST /index, GET /jobs, WS /ws
|   +-- config.py              pydantic Settings (env vars)
|   +-- types.py               EntryKind(13), PipelineJob, ChunkResult, EmbedResult
|   +-- main.py                FastAPI lifespan (runs schema.sql on startup)
|   +-- db/connection.py       psycopg2 ThreadedConnectionPool (min=1, max=10)
|   +-- s3/client.py           boto3 S3 lazy singleton (head_object, download_file)
|   +-- s3/rate_limiter.py     LocalTokenBucket + RedisGlobalTokenBucket
|   +-- cache/hashing.py       file_hash (SHA-256 file), chunk_hash (SHA-256 text)
|   +-- cache/redis_cache.py   is_file_indexed, mark_file_indexed, get/cache_embedding
|   +-- notify/progress.py     Redis pub/sub emit_progress
|   +-- notify/websocket.py    WS handler subscribes to pub/sub
|   +-- pipeline/
|   |   +-- validate.py        MIME check, size check, S3 HEAD
|   |   +-- download.py        rate-limited S3 GET, SHA-256 hash, dedup
|   |   +-- parse/             9 parsers: pdf, docx, docling, image, code, text, data, web, archive
|   |   +-- chunk.py           5 strategies: sliding, code, document, data, single
|   |   +-- embed.py           BGE-M3 via FlagEmbedding (1024-dim), Redis cache
|   |   +-- store.py           pgvector batch upsert, update vault_entries
|   +-- workers/
|       +-- celery_app.py      Celery config (Redis broker)
|       +-- pipeline_task.py   run_pipeline orchestrator, retry logic, clone-on-dedup
+-- tests/                     7 test files (unit + integration)
+-- scripts/                   benchmark.py, bulk_index.py
+-- docker-compose.yml
+-- Dockerfile
+-- Makefile
+-- schema.sql                 vault_entries + vault_chunks (vector(1024))
```

---

## External Dependencies per Module

| Module | S3 | Redis | PostgreSQL | ML Model | Filesystem |
|--------|:--:|:-----:|:----------:|:--------:|:----------:|
| validate.py | HEAD | -- | -- | -- | -- |
| download.py | GET | file cache, rate limit | dedup query | -- | /tmp write |
| parse/* | -- | -- | -- | docling (opt) | /tmp read |
| chunk.py | -- | -- | -- | -- | -- |
| embed.py | -- | embed cache | -- | BGE-M3 (1.5 GB) | -- |
| store.py | -- | mark indexed | INSERT/UPDATE | -- | -- |
| pipeline_task.py | (indirect) | pub/sub | status updates | (indirect) | -- |

---

## Embedding Model

- **Model:** BAAI/bge-m3 (via FlagEmbedding)
- **Dimension:** 1024
- **Tokenizer:** cl100k_base (tiktoken) -- FROZEN
- **Chunk size:** 500 tokens -- FROZEN
- **Overlap:** 50 tokens -- FROZEN
- **Chunker version:** "1"

---

## Dataset

**Location:** `~/Desktop/Uni/cloud-computing/corp_dataset_pipeline/dataset_all/`

| Group | Files | Format | EntryKind | MinIO prefix |
|-------|-------|--------|-----------|-------------|
| dirty PDFs | 101 | Scanned PDFs | DOCUMENT | `dirty/` |
| clean PDFs | 101 | Native PDFs | DOCUMENT | `pdf_native/` |
| DOCX | 101 | Word documents | DOCUMENT | `docx/` |
| XLSX | 101 | Spreadsheets | DATA | `xlsx/` |
| EML | 303 | Emails | UNSORTED | `eml/` |
| PNG | 101 | Scanned images | IMAGE | `png/` |
| **Total** | **808** | | | **~126 MB** |

Manifest: `manifest/manifest.jsonl` (808 records with id, file, format, type, family, minio_key)

---

## Team Infra State (other branches)

| Stack | Branch | Status | Key Resources |
|-------|--------|--------|---------------|
| VPC + Subnets | main | Merged | VPC 10.10.0.0/16, 2 private subnets, Lambda SG, RDS SG |
| KMS + Secrets | main | Merged | 2 CMKs (S3, RDS), DB creds secret, embedding key secret |
| Cognito | main | Merged | User Pool (email, admin-only), App Client |
| API Gateway | main | Merged | REST API, 14 routes (MOCK/501), Cognito authorizer |
| RDS pgvector | feat/infra-5-rds-pgvector | PR open | PostgreSQL 15.7, db.t3.micro, vector(1536) |
| Lambda handlers | michael/api-handler-skeletons | Branch | auth, vault-crud, vault-files (skeleton) |

---

## Known Mismatches (Require Team Decision)

| Issue | Our Code | Michael's Schema | Status |
|-------|----------|-----------------|--------|
| Vector dimension | vector(1024) | vector(1536) | MUST align before RDS merge |
| Chunking model | Flat (500 tok) | Hierarchical (parent+child) | Ship flat now, add later |
| EntryKind count | 13 kinds | 5 entry_types | Not a conflict (different purpose) |
| VPC egress | Lambdas need S3/SQS | No NAT/VPC endpoints | Don't put pipeline Lambdas in VPC |

---

## What Works Now

- `make dev` -- full local stack
- `make test` -- all unit + integration tests pass
- `make bench` -- benchmark with WebSocket stage timings
- `make bulk` -- bulk indexing from vault-test bucket
- POST /index -> full pipeline -> indexed in pgvector
- GET /jobs/{id} -> status polling
- WS /ws/{entry_id} -> real-time progress stream

---

## What Needs to Be Built (My Scope)

- [ ] SQS queue + DLQ (CloudFormation)
- [ ] Step Functions state machine (CloudFormation + ASL)
- [ ] fn-validate Lambda handler
- [ ] fn-download-parse Lambda handler (container image)
- [ ] fn-chunk Lambda handler
- [ ] fn-embed ECS Fargate task
- [ ] fn-store Lambda handler
- [ ] EventBridge rule + SNS topic
- [ ] Dataset upload script (808 files -> S3)
- [ ] Search Lambda (vault-search)
- [ ] DynamoDB sessions table
