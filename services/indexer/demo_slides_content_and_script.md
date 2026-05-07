# Solo Vault Demo Slides: Content And Speaker Script

Use the short bullets for the slide body. Use the script as speaker notes. The flow matches the deck screenshots: title slide, section divider, content slide, repeat.

## Presenter Guardrails

- Keep slide text concise. Let the speaker script carry the implementation details.
- Safe embedding wording for the demo: "vector embeddings stored in pgvector." The codebase currently has conflicting model/dimension notes: root AGENTS says OpenAI `text-embedding-3-small` / 1536, current indexer code uses BGE-M3 with `vector(384)`, and planning docs mention BGE-M3 / 1024. Do not hardcode the model or dimension on the slide unless the team has aligned it.
- Safe chunking wording: "deterministic 500-token chunks with 50-token overlap using `cl100k_base`."

## Slide 1: Solo Vault

**Slide content**

- Cloud-native retrieval infrastructure for the Solo IDE
- Upload, index, and search project knowledge across files and notes
- FastAPI, S3/MinIO, Redis, Celery, PostgreSQL, pgvector

**Speaker script**

Today we are presenting Solo Vault, the retrieval backend for the Solo IDE. The goal is to turn user files, notes, code, and project context into searchable knowledge. The backend accepts vault uploads, processes them asynchronously, stores chunks and vectors in PostgreSQL with pgvector, and streams progress back to the app while indexing is happening.

## Slide 2: Problem Statement Tackled

**Slide content**

Section divider. Keep as-is.

**Speaker script**

I will start with the problem we are solving, then move into motivation, existing alternatives, our architecture, results, future work, and team contributions.

## Slide 3: Problem Statement Tackled

**Slide content**

- Solo IDE needs a cloud-backed memory layer for project knowledge
- Files are heterogeneous: PDFs, DOCX, images, spreadsheets, emails, code, notes
- Indexing is CPU-heavy and should not block the desktop app
- Search must be tenant-scoped, repeatable, and observable

**Speaker script**

The core problem is that useful project context is scattered across many file types. A developer may have PDFs, notes, code snippets, images, spreadsheets, and emails, but the IDE needs one reliable way to retrieve the right context. Local-only indexing is limited by one machine, and synchronous indexing would block the user experience. Solo Vault tackles this by moving file ingestion into an async backend that can validate, parse, chunk, embed, store, and report progress independently from the UI.

## Slide 4: Motivation

**Slide content**

Section divider. Keep as-is.

**Speaker script**

Now that the problem is clear, the motivation is why this matters specifically for an IDE and for cloud computing.

## Slide 5: Motivation

**Slide content**

- Make the IDE context-aware across user and project memory
- Support larger datasets than local indexing alone
- Use cloud primitives for storage, queues, workers, databases, and events
- Build a pipeline that can scale by bottleneck, not as one large process

**Speaker script**

The motivation is to make Solo more context-aware. If the IDE can search a user's own project knowledge, it can answer questions using the files and decisions that actually matter. From a cloud perspective, this is also a good systems problem: uploads belong in object storage, long-running indexing belongs in a queue-backed worker pipeline, embeddings belong in a vector database, and progress updates belong in an event stream. The architecture lets us scale the expensive parts, especially parsing and embedding, instead of scaling the entire backend as one unit.

## Slide 6: Existing Solutions

**Slide content**

Section divider. Keep as-is.

**Speaker script**

Before explaining our design, it helps to compare it with the alternatives we considered.

## Slide 7: Existing Solutions

**Slide content**

- Local IDE index: fast offline, but limited sync and cloud scale
- Cloud drives: store files, but do not create IDE-ready semantic memory
- Generic vector ingestion: useful, but weak on app-specific metadata and progress
- Monolithic worker: simple, but parse and embed cannot scale separately

**Speaker script**

The first alternative is a local-only index. It is fast and works offline, but it does not naturally support cloud sync, shared backend storage, or centralized retrieval. Cloud drives solve storage, but they do not parse and chunk files into IDE-ready memory. Generic vector ingestion systems can embed documents, but they usually miss our app-specific needs like project scope, entry kind, tags, index status, and live progress. Finally, a monolithic worker is easy to build, but parsing and embedding have very different performance profiles, so a single process becomes hard to scale efficiently.

## Slide 8: System Architecture

**Slide content**

Section divider. Keep as-is.

**Speaker script**

Next is the architecture. I will first describe what works locally today, then how the same stages map to AWS.

## Slide 9: System Architecture

**Slide content**

- API: `POST /index`, `GET /jobs/{id}`, `WS /ws/{entry_id}`
- Local stack: FastAPI + Celery worker + Redis + MinIO + PostgreSQL/pgvector
- Pipeline: validate -> download -> parse -> chunk -> embed -> store
- Storage: `vault.entries`, `vault.chunks`, pgvector index, full-text table
- AWS path: S3 -> SQS -> Step Functions -> Lambda/ECS stages -> RDS

**Speaker script**

The current local backend starts with FastAPI. When the app calls `POST /index`, the API writes a pending entry, then enqueues a Celery job through Redis. The worker runs six stages: validate the MIME type, size, and object existence; download through a rate-limited S3 client; parse based on entry kind; chunk with deterministic token rules; embed each chunk; and store the final rows in PostgreSQL with pgvector. Redis is also used for file and chunk caches, plus progress pub/sub. The AWS version keeps the same logical stages but splits them: S3 upload events go to SQS, Step Functions orchestrates each stage, Lambda handles lightweight stages, ECS handles embedding, and RDS stores the final searchable state.

## Slide 10: Future Work

**Slide content**

Section divider. Keep as-is.

**Speaker script**

Now I will cover what remains after the demo version and what we would improve next.

## Slide 11: Future Work

**Slide content**

- Finish and verify Step Functions deployment path
- Align embedding model and vector dimension across IDE, backend, and RDS
- Add production auth enforcement from Cognito JWT claims
- Complete remote search API and hybrid lexical + semantic retrieval
- Add CloudWatch dashboard, alerts, and DLQ handling for failed jobs

**Speaker script**

The main future work is production hardening. The local pipeline and extracted Lambda/ECS handlers exist, but the AWS deployment path still needs final verification. We also need to align the embedding model and vector dimension across the IDE, backend, and RDS schema before production indexing, because that choice affects all stored vectors. Another important improvement is auth enforcement: production routes should derive user identity from Cognito JWT claims instead of trusting a client-supplied user ID. After that, the search API can combine pgvector semantic search with full-text search, and the operational layer can add CloudWatch dashboards, alarms, and dead-letter queue handling.

## Slide 12: Results

**Slide content**

Section divider. Keep as-is.

**Speaker script**

The results slide focuses on what we can demonstrate and what the codebase already supports.

## Slide 13: Results

**Slide content**

- Working local stack with app, worker, Redis, MinIO, and pgvector Postgres
- End-to-end index flow: queued -> extracting -> indexed or failed
- Real-time progress events for validate, download, parse, chunk, embed, store
- Test coverage for routes, parsing, chunking, caching, validation, and pipeline paths
- Dataset tooling for 808 files across six groups

**Speaker script**

The result is a working local indexing service. We can submit an entry, watch the job move through the pipeline, and see chunks stored in PostgreSQL. The status API reports job state and chunk count, and the WebSocket streams progress events for each major stage. The tests cover route behavior, parsing, chunking, validation, Redis cache behavior, idempotency, missing S3 objects, and full pipeline paths for PDFs, DOCX, PNG, and unsorted email files. The repo also includes scripts for benchmarking and bulk indexing the 808-file test dataset across PDFs, DOCX, XLSX, emails, and images.

## Slide 14: Team Contributions

**Slide content**

Section divider. Keep as-is.

**Speaker script**

Finally, I will summarize how the work was divided across the team.

## Slide 15: Team Contributions

**Slide content**

- Cloud foundation: VPC, networking, Cognito, API Gateway
- Security and data layer: KMS, Secrets Manager, RDS PostgreSQL, pgvector schema
- Indexing pipeline: validation, parsing, chunking, embedding, storage, progress
- AWS orchestration: SQS, Step Functions, Lambda/ECS extraction, EventBridge/SNS
- Demo and QA: dataset upload, benchmarks, integration tests, documentation

**Speaker script**

The project was split across cloud foundation, data infrastructure, pipeline implementation, orchestration, and demo readiness. The foundation work includes networking, Cognito, and API Gateway. The data layer includes KMS, Secrets Manager, RDS PostgreSQL, and pgvector. The indexing work covers the actual document pipeline: validation, rate-limited download, parser dispatch, deterministic chunking, embedding, transactional storage, and progress events. The AWS orchestration work maps that pipeline onto SQS, Step Functions, Lambda, ECS, EventBridge, and SNS. Finally, the demo and QA work includes the dataset upload flow, benchmark scripts, integration tests, and documentation.

## Optional Q&A Notes

- **Why pgvector?** It lets us keep metadata, chunks, lexical search, and vector search in PostgreSQL instead of adding a separate vector database.
- **Why Redis?** It serves as the Celery broker, a chunk/file cache, a distributed S3 rate-limit coordinator, and a local progress pub/sub channel.
- **Why async indexing?** Parsing and embedding can be slow, especially for OCR and large documents, so the app should receive progress rather than block.
- **Why Step Functions later?** It gives per-stage retries, failure isolation, execution history, and a strong visual demo of the pipeline.
- **What is the key risk?** Embedding model/dimension alignment must be finalized before production data is indexed.

## Source Files Checked

- `services/indexer/README.md`
- `services/indexer/state.md`
- `services/indexer/plan.md`
- `services/indexer/app/api/routes.py`
- `services/indexer/app/workers/pipeline_task.py`
- `services/indexer/app/pipeline/validate.py`
- `services/indexer/app/pipeline/download.py`
- `services/indexer/app/pipeline/chunk.py`
- `services/indexer/app/pipeline/embed.py`
- `services/indexer/app/pipeline/store.py`
- `services/indexer/app/db/schema.sql`
- `services/indexer/app/notify/progress.py`
- `services/indexer/app/notify/websocket.py`
- `services/indexer/lambdas/fn_validate/handler.py`
- `services/indexer/lambdas/fn_download_parse/handler.py`
- `services/indexer/lambdas/fn_chunk/handler.py`
- `services/indexer/lambdas/fn_embed/entrypoint.py`
- `services/indexer/lambdas/fn_store/handler.py`
- `services/indexer/tests/test_routes.py`
- `services/indexer/tests/test_pipeline.py`
- `docs/API.md`
- `docs/decision.md`
- `docs/WORK_BREAKDOWN.md`
- `docs/AWS_VAULT_SERVICE_HANDOFF.md`
