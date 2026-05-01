# Architecture Decision Record — Solo Vault Indexing Pipeline

**Date:** 2026-04-23
**Author:** Engineer 3 (Pipeline & Data)
**Status:** PROPOSED — pending team review

---

## Table of Contents

1. [Context: What We're Solving](#1-context)
2. [Decision-Making Framework](#2-decision-making-framework)
3. [AWS Design Principles Applied](#3-aws-design-principles)
4. [Current State: The Monolith](#4-current-state)
5. [Target State: Three Architecture Options](#5-three-options)
6. [Trade-Off Matrix](#6-trade-off-matrix)
7. [The Big File Problem: Data Between Stages](#7-big-file-problem)
8. [Taxonomy: EntryKind Classification](#8-taxonomy)
9. [Data Flow Per Pipeline Stage](#9-data-flow)
10. [Team Alignment: Open Questions](#10-team-alignment)
11. [Recommendation](#11-recommendation)
12. [Appendix: Agentic Design Patterns Analogy](#12-agentic-patterns)

---

## 1. Context: What We're Solving <a id="1-context"></a>

Solo Vault is a **cloud-backed knowledge store** for the Solo IDE desktop app.
Users upload documents (PDFs, code, images, spreadsheets, emails),
and the system indexes them into vector embeddings for semantic search.

The indexing pipeline currently runs as a **monolithic microservice**:
Celery + FastAPI + BGE-M3 embedding model, all inside one Docker container.

**Problem:** This monolith doesn't scale. One Celery worker processes files
sequentially. To index 808 files takes hours. We need to split it into
independently-scalable cloud services.

**Constraints:**
- Team of 4 engineers, university cloud computing course
- Budget: minimize cost, scale to zero when idle
- Dataset: 808 SEC filing documents (126 MB, 6 formats)
- Must use 15 AWS services (per project requirements)
- Embedding model: BAAI/bge-m3 (1024-dim, ~1.5 GB model file)

### What Teammates Have Already Built

| Stack | Engineer | Status | AWS Resources |
|-------|----------|--------|---------------|
| VPC + Subnets + SGs | Sachin | Merged | EC2 VPC, 2 Private Subnets, Lambda SG, RDS SG |
| Cognito (auth) | Sachin | Merged | User Pool, App Client |
| API Gateway | Sachin | Merged | REST API, Cognito Authorizer, 14 Endpoints (MOCK) |
| KMS + Secrets Manager | Michael | Merged | 2 KMS Keys, DB Credentials, Embedding API Key |
| RDS PostgreSQL + pgvector | Michael | PR (not merged) | db.t3.micro, PostgreSQL 15.7, pgvector |
| Lambda Handlers (skeleton) | Michael | Branch | auth-handler, vault-crud, vault-files |

### My Scope (Engineer 3)
- SQS Queue (index queue + DLQ)
- Step Functions State Machine (pipeline orchestration)
- Pipeline Lambda/ECS functions (validate, download+parse, chunk, embed, store)
- EventBridge + SNS (progress notifications)
- Search Lambda
- DynamoDB (sessions)
- Dataset upload to S3

---

## 2. Decision-Making Framework <a id="2-decision-making-framework"></a>

Before evaluating options, we need principles for **how** to decide.
Otherwise we end up bikeshedding endlessly.

### Principle 1: Reversibility

> "If a decision is easily reversible, make it quickly.
> If it's irreversible, think carefully."
> — Jeff Bezos, "Type 1 vs Type 2 Decisions"

**Applied to our choices:**

| Decision | Reversible? | Consequence of getting it wrong |
|----------|------------|--------------------------------|
| Step Functions vs SQS chains | Yes — can switch in a day | Wasted implementation time |
| Lambda vs ECS for a stage | Yes — swap compute, keep logic | Minor refactor |
| Embedding model (BGE-M3 vs OpenAI) | **NO** — requires re-indexing all data + schema change | Days of work to undo |
| Vector dimension (1024 vs 1536) | **NO** — baked into DB schema, IVFFlat index, all queries | Days of work to undo |
| Chunking strategy (flat vs hierarchical) | Partially — requires re-index but not schema change | Hours of re-processing |

**Decision:** Focus our deliberation time on **irreversible** choices
(embedding model, vector dimension). For everything else, pick the
standard AWS pattern and move on.

### Principle 2: Optimize for the Bottleneck

> "Any improvement not at the bottleneck is an illusion."
> — Eliyahu Goldratt, Theory of Constraints

Current bottleneck analysis:

| Pipeline Stage | Time per file | Bottleneck type |
|----------------|--------------|-----------------|
| validate | ~50ms | Network (S3 HEAD) |
| download | ~200ms | Network (S3 GET) |
| parse | 1-70s | **CPU** (OCR, docling) |
| chunk | ~50ms | CPU (tokenization) |
| embed | 0.05-214s | **CPU** (BGE-M3 inference) |
| store | ~100ms | Network (Postgres) |

**Parse** and **embed** are the bottlenecks. The architecture must allow
these two stages to scale independently. This is the primary reason
to split the monolith — not because microservices are fashionable,
but because the bottleneck stages need independent scaling.

### Principle 3: Cost Follows Usage

> "Pay only for what you use. Scale to zero when idle."
> — AWS Well-Architected Cost Optimization Pillar

For a university project with bursty usage (upload 808 files, then idle
for days), idle cost matters more than per-request cost. An ECS service
running 24/7 at $30/month is worse than Lambda at $0/month idle,
even if Lambda costs slightly more per invocation.

---

## 3. AWS Design Principles Applied <a id="3-aws-design-principles"></a>

### From the AWS Well-Architected Framework

| Principle | Application to Our System |
|-----------|--------------------------|
| **Stop guessing capacity** | Lambda auto-scales. No capacity planning needed for parse/chunk/store. ECS Fargate for embed scales based on queue depth. |
| **Test at production scale** | Upload all 808 files at once. Step Functions Map state processes them in parallel. |
| **Consider evolutionary architectures** | Start with flat chunking (what works now). Add hierarchical chunking later without changing the pipeline structure. |
| **Drive architectures using data** | Step Functions execution history shows per-stage timings. Use this data to find and fix bottlenecks. |

### From the AWS Serverless Application Lens

| Principle | Application |
|-----------|------------|
| **Speedy, simple, singular** | Each Lambda does ONE thing: validate, or parse, or chunk, or store. No multi-purpose functions. |
| **Think concurrent requests, not total** | 808 files = 808 concurrent Step Functions executions. Each file processes independently. |
| **Share nothing** | No shared state between Lambda invocations. All state flows through Step Functions payload or S3 intermediate storage. |
| **Orchestrate with state machines, not functions** | Step Functions orchestrates the pipeline. Lambdas don't call each other. No Lambda-to-Lambda chaining. |
| **Use events to trigger transactions** | S3 ObjectCreated event triggers the pipeline. No polling, no cron jobs. |
| **Design for failures and duplicates** | Each stage is idempotent. `entry_id` is the idempotency key. Re-processing the same file produces the same result. |

### From AWS Lambda Best Practices

| Practice | Application |
|----------|------------|
| **Initialize outside handler** | DB connections, S3 clients, tokenizer — initialized at cold start, reused across invocations. |
| **Write idempotent code** | `ON CONFLICT DO NOTHING` for chunk inserts. `entry_id` is the dedup key. |
| **Use environment variables** | Bucket name, DB secret ARN, embedding model name — all from env vars, not hardcoded. |
| **Avoid recursive invocations** | Lambdas don't invoke other Lambdas. Step Functions handles the flow. |

---

## 4. Current State: The Monolith <a id="4-current-state"></a>

```
POST /index -> Redis (Celery broker) -> Celery Worker:
  validate -> download -> parse -> chunk -> embed -> store
  (all in one process, one container)
```

**What works well:**
- Simple — one Docker image, one deploy
- Fast inter-stage data transfer — everything in memory
- BGE-M3 model loaded once, stays warm
- 6 pipeline stages with progress events via Redis pub/sub

**What doesn't work:**
- Sequential processing (1 file at a time per worker)
- Can't scale stages independently (embed is 100x slower than validate)
- Paying for idle ECS capacity 24/7
- Single point of failure (container crash = all jobs fail)
- No visual pipeline monitoring

---

## 5. Target State: Three Architecture Options <a id="5-three-options"></a>

### Option A: Pragmatic — ECS Monolith + AWS Wrappers

Ship the existing Docker container on ECS Fargate. Add SQS trigger
for S3 events. Keep Celery pipeline as-is.

```
S3 Event -> SQS -> ECS Fargate (Celery + FastAPI + BGE-M3)
                    +-- validate -> download -> parse -> chunk -> embed -> store
```

**AWS Services (10):** API Gateway, Cognito, Lambda x3, S3, SQS,
ECS Fargate, RDS, Secrets Manager, KMS, CloudWatch

### Option B: Recommended — Step Functions + Lambda + ECS Embed

Split pipeline into 4 Lambda stages + 1 ECS Fargate task (embed).
Step Functions orchestrates. SQS buffers S3 events.

```
S3 Event -> SQS -> Step Functions:
  +-- Lambda: Validate
  +-- Lambda: Download + Parse
  +-- Lambda: Chunk
  +-- ECS Fargate Task: Embed (BGE-M3, .sync integration)
  +-- Lambda: Store

Step Functions state change -> EventBridge -> SNS -> ws-notify Lambda
```

**AWS Services (15):** API Gateway (REST + WebSocket), Cognito,
Lambda x9, S3, SQS, Step Functions, ECS Fargate, RDS, ElastiCache,
DynamoDB, EventBridge, SNS, Secrets Manager, KMS, CloudWatch

### Option C: Full Serverless — All Lambda + API Embedding

Replace BGE-M3 with OpenAI API calls. All stages run on Lambda.
No ECS, no ElastiCache.

```
S3 Event -> SQS -> Step Functions:
  +-- Lambda: Validate
  +-- Lambda: Download + Parse
  +-- Lambda: Chunk
  +-- Lambda: Embed (OpenAI API call)
  +-- Lambda: Store
```

**AWS Services (15):** Same as B but without ECS Fargate and ElastiCache.
Adds external OpenAI API dependency.

---

## 6. Trade-Off Matrix <a id="6-trade-off-matrix"></a>

| Dimension | Option A: ECS Monolith | Option B: SFN + Lambda + ECS | Option C: Full Lambda |
|-----------|----------------------|------------------------------|----------------------|
| **AWS Services** | 10 | **15** | 15 |
| **Implementation effort** | Low (reuse code) | High (refactor 5 stages) | Medium (refactor 5 stages) |
| **Idle cost/month** | ~$30 (Fargate 24/7) | **~$5** (only RDS) | **~$5** (only RDS) |
| **808-file batch cost** | ~$0.80 | ~$1.60 | ~$2.40 + ~$0.03 OpenAI |
| **Max throughput** | ~10 files/min | **~200 files/min** | ~500 files/min |
| **Scale to zero** | No | Partial (ECS cold start) | **Yes (100%)** |
| **Demo quality** | Low | **High** (SFN console) | High (SFN console) |
| **Failure isolation** | None (all-or-nothing) | **Per-stage** | Per-stage |
| **Observability** | Logs only | **Per-stage metrics + history** | Per-stage metrics |
| **BGE-M3 compatible** | Yes | **Yes** | **No** (breaks IDE parity) |
| **External dependency** | None | None | **OpenAI API** |
| **Cold start** | None | ~30s (ECS embed) | <1s (all Lambda) |

---

## 7. The Big File Problem: Data Between Stages <a id="7-big-file-problem"></a>

### The Problem

In a monolith, data stays in memory. When you split into Lambda functions,
data must be **serialized and transferred** between stages.

Step Functions payload limit: **256 KB**.

What exceeds 256 KB:
- `extracted_text` from a large PDF: up to 500 KB
- `embeddings[]` array: 100 chunks x 4 KB per vector = 400 KB+

### The Solution: "S3 as the Data Bus" Pattern

This is the standard AWS pattern for large-payload Step Functions workflows:

```
If payload < 200 KB:
  -> Pass inline in Step Functions JSON payload (fast, free)

If payload > 200 KB:
  -> Write to S3: s3://bucket/pipeline/{entry_id}/{stage}.json
  -> Pass S3 reference in payload: { "s3_ref": "pipeline/e1/text.json" }
  -> Next stage reads from S3 (adds ~100ms latency, ~$0.000005 cost)
```

**Per stage:**

| Stage | Input source | Output destination | Reason |
|-------|-------------|-------------------|--------|
| Validate | SFN payload (1 KB) | SFN payload (1 KB) | Always small |
| Download+Parse | SFN payload + S3 (file) | SFN payload OR **S3** | Text may exceed 256 KB |
| Chunk | SFN payload or S3 ref | SFN payload OR **S3** | Chunks array usually fits |
| Embed (ECS) | SFN payload or S3 ref | **Always S3** | Embeddings always large |
| Store | S3 ref | RDS (final destination) | Reads from S3, writes to DB |

**Cleanup:** Store Lambda deletes `s3://bucket/pipeline/{entry_id}/*`
after successful write to RDS.

### Why Not Split the Source File?

No. The source file (PDF, DOCX, image) is NOT the problem:
- Lambda `/tmp` supports up to 10 GB — a 50 MB file fits easily
- Lambda timeout is 15 minutes — enough for any parser

The problem is the **output** of parsing (extracted text) and embedding
(float vectors), not the input file itself. The S3 data bus pattern
solves this without touching the source file.

**Exception:** ZIP archives — these ARE split, using Step Functions
Map state to process each member file in parallel.

### Cost of Intermediate S3

For 808 files:
- ~808 PUT + ~808 GET + ~808 DELETE = ~2424 requests
- Cost: ~$0.01 (less than one cent)
- Data lives seconds (created -> read -> deleted)

---

## 8. Taxonomy: EntryKind Classification <a id="8-taxonomy"></a>

### Why a Taxonomy?

Different file types need different processing strategies at every
pipeline stage. The `EntryKind` enum routes files to the right
parser and chunker.

### The 13 EntryKind Values

| Kind | Parser | Chunker | Why separate? |
|------|--------|---------|---------------|
| `DOCUMENT` | docling/pypdf/docx | Paragraph-aware packing (500 tok) | Most common; layout-aware extraction |
| `CODE` | Raw text + language header | Block-split on def/class/fn | Functions should stay in one chunk |
| `SNIPPET` | Same as CODE | Same as CODE | Shorter fragments |
| `IMAGE` | pytesseract OCR | Single chunk | OCR is slow; images rarely produce long text |
| `DESIGN` | pytesseract OCR | Single chunk | Same as IMAGE; separate for metadata filtering |
| `DATA` | CSV schema + rows / JSON keys | Schema in chunk 0, row batches | Schema must be preserved for context |
| `CONFIG` | Same as DATA | Same as Data | Separate from DATA for filtering |
| `WEB` | BeautifulSoup strip scripts | Paragraph-aware packing | HTML needs tag stripping |
| `NOTE` | Raw UTF-8 | Paragraph-aware packing | Simplest path |
| `KEYVALUE` | Raw UTF-8 | Sliding window | Simple text, no structure |
| `AUDIO` | (Future: Whisper) | (Future) | Not yet implemented |
| `ARCHIVE` | ZIP extract -> recursive parse | Per-member chunks | Recursive extraction |
| `UNSORTED` | Best-effort UTF-8 | Sliding window | Fallback (classifier_confidence < 0.6) |

### Taxonomy Flow Through Pipeline

```
File upload -> classifier assigns kind + confidence
  |
  if confidence < 0.6 -> kind = UNSORTED
  |
  Validate: MIME check by kind (CODE gets extension bypass)
  Parse:    Dispatch to parser by kind
  Chunk:    Dispatch to chunker by kind
  Embed:    Same for all kinds (BGE-M3)
  Store:    Same for all kinds (pgvector) -- kind stored as metadata
```

### Alignment with Teammate's Schema

Michael's `vault-crud` handler uses 5 `entry_type` values:
`note | file | snippet | config | keyvalue`

Our indexer uses 13 `EntryKind` values. These are NOT in conflict --
`entry_type` is the user-facing category, `EntryKind` is the
pipeline-internal processing classification. The pipeline reads
`kind` from the index request and uses it for parser/chunker dispatch.

---

## 9. Data Flow Per Pipeline Stage <a id="9-data-flow"></a>

### Stage 1: Validate

```
Input:  { entry_id, s3_key, bucket, mime, kind, size_bytes, ... }  ~1 KB
Logic:  Check MIME in ALLOWED_MIMES (22 types)
        Check size_bytes <= 50 MB
        S3 HEAD -> verify object exists
        If S3 404 -> status="deleted", stop pipeline
Output: Same payload, validated  ~1 KB
Transfer: SFN payload (always fits)
```

### Stage 2: Download + Parse

```
Input:  Validated job metadata  ~1 KB
Logic:  S3 GET -> Lambda /tmp/{entry_id}_{filename}  (rate-limited)
        SHA-256 hash -> dedup check (Redis/DB)
        Kind-dispatched text extraction (see taxonomy)
        Normalize whitespace
Output: { extracted_text: str, file_hash: str }  100B - 500KB
Transfer: SFN payload if <200KB, else S3 intermediate
```

### Stage 3: Chunk

```
Input:  extracted_text (inline or S3 ref)  up to 500KB
Logic:  Tokenizer: cl100k_base (tiktoken)
        Chunk size: 500 tokens, Overlap: 50 tokens
        Kind-dispatched chunking strategy
Output: { chunks: [{ index, content, token_count }] }  2KB - 200KB
Transfer: SFN payload if <200KB, else S3 intermediate
```

### Stage 4: Embed (ECS Fargate)

```
Input:  chunks[] (inline or S3 ref)  up to 200KB
Logic:  BGE-M3 batch inference (batch_size=32)
        Per-chunk hash -> Redis cache check (30-day TTL)
        Cache misses -> model.encode()
        Cache new embeddings
Output: { embeddings: [{ index, content, vector[1024] }] }  6KB - 600KB
Transfer: ALWAYS S3 (embeddings always exceed 256KB for real docs)
```

### Stage 5: Store

```
Input:  embeddings[] from S3  up to 600KB
Logic:  BEGIN TRANSACTION
          DELETE FROM vault_chunks WHERE entry_id = ?
          INSERT INTO vault_chunks (entry_id, user_id, chunk_index,
                     content, embedding::vector, token_count, chunk_hash)
          UPDATE vault_entries SET index_status='indexed', chunk_count=N
        COMMIT
        Cleanup: DELETE s3://bucket/pipeline/{entry_id}/*
Output: Nothing (writes to DB, emits final progress event)
```

---

## 10. Team Alignment: Open Questions <a id="10-team-alignment"></a>

These are **irreversible decisions** that require team agreement
before implementation begins:

### Question 1: Vector Dimension

Michael's RDS schema uses `vector(1536)` (OpenAI embedding size).
Our indexer uses BGE-M3 at `vector(1024)`.

| Option | Pros | Cons |
|--------|------|------|
| `1024` (BGE-M3) | Code works now. No API cost. IDE-compatible. | Need to change Michael's schema PR. |
| `1536` (OpenAI) | Matches AGENTS.md contract. Better search quality (debatable). | Must switch model. Per-token API cost. Breaks IDE parity. |

**Recommendation:** `1024`. The model works, the code exists,
and switching models close to deadline is high-risk.

### Question 2: Chunking Model

Michael's schema has hierarchical parent/child chunks + BM25 tsvector.
Our indexer uses flat 500-token chunks.

| Option | Pros | Cons |
|--------|------|------|
| Flat (current) | Works now. Simple. Matches IDE. | Less sophisticated retrieval |
| Hierarchical | Better retrieval quality | Must rewrite chunker. Adds complexity. |

**Recommendation:** Ship flat chunks now. The schema can support
hierarchical chunks later -- it's an additive change.

### Question 3: VPC Internet Egress

Lambdas in private subnets have no internet access (no NAT Gateway,
no VPC endpoints). This blocks:
- S3 access from Lambda
- Secrets Manager access from Lambda
- Step Functions callback
- CloudWatch logging

**Solutions:**
- NAT Gateway: ~$32/month + $0.045/GB
- VPC Endpoints for S3, Secrets Manager, SQS, SFN: ~$7/month each
- Or: Don't put Lambdas in VPC (only RDS needs VPC)

**Recommendation:** Don't put pipeline Lambdas in VPC.
Only the Store Lambda needs VPC access (to reach RDS).
Other Lambdas access S3 and SQS over public endpoints.

---

## 11. Recommendation <a id="11-recommendation"></a>

### Decision: Option B — Step Functions + Lambda + ECS Embed

**Reasons:**

1. **Bottleneck optimization** — Parse and Embed scale independently.
   808 files process in minutes instead of hours.

2. **15 AWS services** — Maximum coverage for project grading.

3. **Demo quality** — Step Functions console shows each stage
   executing in real-time. Per-stage metrics in CloudWatch.

4. **AWS-blessed pattern** — S3 event -> SQS -> Step Functions -> Lambda/ECS
   is the documented AWS pattern for async document processing.
   Step Functions has native `.sync` ECS integration.

5. **Cost-efficient** — Scale to zero for Lambda stages.
   ECS Fargate only runs during embedding (pay per second).

6. **Failure isolation** — Each stage retries independently.
   Parse failure doesn't lose download work. Embed failure
   doesn't re-parse the document.

7. **Reversibility** — If ECS embed causes issues, can temporarily
   swap to OpenAI API Lambda (Option C fallback) without changing
   the pipeline structure.

### Embedding Model: BGE-M3 (1024-dim)

- Already works in current codebase
- No external API dependency
- Compatible with IDE's local sqlite-vec index
- Michael's schema PR should update `vector(1536)` -> `vector(1024)`

### Chunking: Flat (500 tokens, 50 overlap)

- Ship what works now
- Hierarchical chunking is a future enhancement
- Schema is forward-compatible (can add `vault_chunk_parents` later)

---

## 12. Appendix: Agentic Design Patterns Analogy <a id="12-agentic-patterns"></a>

Andrew Ng identified 4 agentic design patterns (2024) for AI systems.
Interestingly, each maps to a component in our pipeline architecture:

| Agentic Pattern | Our System Equivalent | How It Applies |
|----------------|----------------------|----------------|
| **Reflection** | Validate stage | System inspects input before processing. Checks MIME, size, S3 existence. Rejects invalid work early -- "reflect before acting." |
| **Tool Use** | Parse stage (kind-dispatched) | Different "tools" for different tasks: pytesseract for OCR, docling for PDF, BeautifulSoup for HTML. The pipeline selects the right tool based on EntryKind. |
| **Planning** | Step Functions state machine | The entire state machine IS a plan: "first validate, then download, then parse, then chunk, then embed, then store." Explicit, auditable, with retry/fallback paths. |
| **Multi-Agent Collaboration** | Lambda/ECS per stage | Each stage is an independent "agent" with a single responsibility. They don't know about each other. They communicate through a shared protocol (Step Functions payload / S3 intermediate). The orchestrator (Step Functions) coordinates them. |

This analogy is not accidental. Well-designed distributed systems and
well-designed AI agent systems solve the same fundamental problem:
**coordinating independent, specialized workers on a complex task
with clear handoff protocols and failure handling.**

### The Deeper Lesson

The monolith is like zero-shot prompting -- you ask the system to do
everything in one pass, start to finish, no revision. It works
surprisingly well for simple cases.

The Step Functions pipeline is like an agentic workflow -- each stage
can fail, retry, and produce intermediate results. The orchestrator
manages the flow. You get better results through iteration and
specialization, not through building a bigger monolith.

---

## References

- [AWS Well-Architected Framework -- General Design Principles](https://docs.aws.amazon.com/wellarchitected/latest/framework/general-design-principles.html)
- [AWS Serverless Application Lens -- Design Principles](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/general-design-principles.html)
- [AWS Lambda Best Practices](https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html)
- [AWS Step Functions -- ECS/Fargate .sync Integration](https://docs.aws.amazon.com/step-functions/latest/dg/connect-ecs.html)
- [Andrew Ng -- Agentic Design Patterns (2024)](https://www.deeplearning.ai/the-batch/how-agents-can-improve-llm-performance/)
- [Jeff Bezos -- Type 1 vs Type 2 Decisions](https://www.sec.gov/Archives/edgar/data/1018724/000119312516530910/d168744dex991.htm)
- [Eliyahu Goldratt -- Theory of Constraints](https://en.wikipedia.org/wiki/Theory_of_constraints)
