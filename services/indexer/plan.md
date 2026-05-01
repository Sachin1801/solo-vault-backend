# Migration Plan: Monolith -> Step Functions + Lambda + ECS

## Overview

Split the Celery-based indexing monolith into 5 independently-deployable
units orchestrated by AWS Step Functions.

```
BEFORE (monolith):
  Celery worker -> validate -> download -> parse -> chunk -> embed -> store
  (all in one process)

AFTER (distributed):
  S3 Event -> SQS -> Step Functions:
    +-- Lambda: fn-validate        (app/pipeline/validate.py)
    +-- Lambda: fn-download-parse  (app/pipeline/download.py + parse/)
    +-- Lambda: fn-chunk           (app/pipeline/chunk.py)
    +-- ECS Fargate: fn-embed      (app/pipeline/embed.py)
    +-- Lambda: fn-store           (app/pipeline/store.py)
```

---

## Phase 0: Shared Infrastructure (CloudFormation)

### 0.1 -- SQS Queue

**File:** `infra/cloudformation/sqs-pipeline.yml`

| Resource | Type | Config |
|----------|------|--------|
| `VaultIndexQueue` | `AWS::SQS::Queue` | VisibilityTimeout: 900s, Retention: 4 days |
| `VaultIndexDLQ` | `AWS::SQS::Queue` | Retention: 14 days |
| `RedrivePolicy` | on VaultIndexQueue | maxReceiveCount: 3 |

### 0.2 -- Step Functions State Machine

**File:** `infra/cloudformation/step-functions-pipeline.yml`
**ASL:** `infra/step-functions/pipeline.asl.json`

5 states: Validate -> DownloadParse -> Chunk -> Embed -> Store
Error states: MarkDeleted, MarkFailed, CloneFromSource

### 0.3 -- EventBridge + SNS

**File:** `infra/cloudformation/notifications-pipeline.yml`

EventBridge rule on SFN state changes -> SNS topic -> ws-notify Lambda

---

## Phase 1: Lambda Extraction Map

### Source -> Target mapping

| Monolith module | Target | Runtime | Memory | Timeout | VPC? |
|----------------|--------|---------|--------|---------|------|
| `pipeline/validate.py` | Lambda `fn-validate` | Python 3.12 zip | 256 MB | 30s | No |
| `pipeline/download.py` + `pipeline/parse/*` | Lambda `fn-download-parse` | Python 3.12 container | 2048 MB | 15 min | Yes (ElastiCache) |
| `pipeline/chunk.py` | Lambda `fn-chunk` | Python 3.12 zip | 512 MB | 60s | No |
| `pipeline/embed.py` | ECS Fargate task | Docker image | 4096 MB | -- | Yes (ElastiCache) |
| `pipeline/store.py` | Lambda `fn-store` | Python 3.12 zip | 512 MB | 60s | Yes (RDS) |

### Dependency matrix (what each handler needs)

| Handler | boto3/S3 | Redis | PostgreSQL | ML model | tiktoken | filesystem |
|---------|:--------:|:-----:|:----------:|:--------:|:--------:|:----------:|
| fn-validate | HEAD | -- | -- | -- | -- | -- |
| fn-download-parse | GET | cache, rate-limit | dedup query | docling (opt) | -- | /tmp |
| fn-chunk | -- | -- | -- | -- | yes | -- |
| fn-embed (ECS) | GET/PUT | embed cache | -- | BGE-M3 1.5GB | -- | -- |
| fn-store | PUT/DELETE | mark indexed | INSERT/UPDATE | -- | -- | -- |

---

## Phase 2: Handler Implementation

### 2.1 -- fn-validate

**File:** `services/indexer/lambdas/fn_validate/handler.py`
**Extraction effort:** ~1 hour

Reuses: `app.pipeline.validate`, `app.types`, `app.s3.client`
Removes: all Redis/DB imports
Handler: deserialize event -> call `validate(job)` -> return event

### 2.2 -- fn-download-parse

**File:** `services/indexer/lambdas/fn_download_parse/handler.py`
**Extraction effort:** ~4 hours (heaviest -- includes all 9 parsers)

Reuses: `app.pipeline.download`, `app.pipeline.parse`, `app.cache.*`, `app.s3.*`
S3 data bus: if `extracted_text` > 200 KB -> write to `pipeline/{entry_id}/text.json`
Container image: needs poppler-utils, tesseract-ocr system packages

### 2.3 -- fn-chunk

**File:** `services/indexer/lambdas/fn_chunk/handler.py`
**Extraction effort:** ~1 hour (cleanest -- zero external deps beyond tiktoken)

Reuses: `app.pipeline.chunk`, `app.types`
Reads text from SFN payload or S3 ref
S3 data bus: if chunks > 200 KB -> write to `pipeline/{entry_id}/chunks.json`

### 2.4 -- fn-embed (ECS Fargate)

**File:** `services/indexer/lambdas/fn_embed/entrypoint.py`
**Extraction effort:** ~3 hours

Docker image with BGE-M3 model pre-downloaded
Reads chunks from S3, writes embeddings to S3 (ALWAYS -- too large for SFN)
ECS task def: 2 vCPU, 4 GB, Fargate, on-demand via SFN `.sync`

### 2.5 -- fn-store

**File:** `services/indexer/lambdas/fn_store/handler.py`
**Extraction effort:** ~2 hours

Multi-action handler: store / mark_indexed / mark_deleted / mark_failed / clone
Reads embeddings from S3 -> pgvector upsert -> cleanup intermediate files
Refactor: replace ThreadedConnectionPool with single psycopg2.connect() + RDS Proxy

---

## Phase 3: Dataset Upload

**File:** `services/indexer/scripts/upload_dataset.py`

Reads manifest.jsonl -> uploads 808 files to S3 -> optionally triggers SFN

| Group | Files | S3 prefix | EntryKind |
|-------|-------|-----------|-----------|
| dirty PDFs | 101 | `dirty/` | DOCUMENT |
| clean PDFs | 101 | `pdf_native/` | DOCUMENT |
| DOCX | 101 | `docx/` | DOCUMENT |
| XLSX | 101 | `xlsx/` | DATA |
| EML | 303 | `eml/` | UNSORTED |
| PNG | 101 | `png/` | IMAGE |

---

## Phase 4: Search Lambda

**File:** `services/indexer/lambdas/fn_search/handler.py`

1. Embed query (call ECS or cached)
2. pgvector cosine similarity: `ORDER BY embedding <=> $query_vec`
3. Filter by user_id + project_id
4. Return top-K above threshold

---

## Execution Order

```
Phase 0  ->  CloudFormation (SQS, SFN, EventBridge/SNS)
Phase 1  ->  ASL definition (pipeline.asl.json)
Phase 2  ->  Lambda handlers (parallel):
               2.3 fn-chunk      (~1 hr)  <- start here (easiest)
               2.1 fn-validate   (~1 hr)
               2.5 fn-store      (~2 hrs)
               2.2 fn-download-parse (~4 hrs)
               2.4 fn-embed ECS  (~3 hrs)
Phase 3  ->  Dataset upload + smoke test
Phase 4  ->  Search Lambda
```

**Total estimated effort:** ~15-20 hours

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| ECS embed cold start (30s) | Slow first file | SQS buffers; batch multiple files |
| Docling in Lambda container (~3 GB image) | Slow deploy | Fallback to pypdf only |
| RDS connection limits | Lambda concurrency exhausts pool | Use RDS Proxy |
| SFN 256 KB payload limit | Large PDFs break pipeline | S3 data bus pattern |
| ElastiCache cost (~$13/mo) | Budget | Optional: DynamoDB TTL cache |
