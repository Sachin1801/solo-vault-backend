# Solo Vault AWS Service Handoff

This document is the implementation handoff for connecting Solo's local Vault
feature to the separate AWS Vault service.

## Goal

Move new Vault uploads, indexing, and remote search from Solo's local SQLite
implementation into the AWS Vault backend service.

For v1, existing local SQLite Vault rows stay local/offline. Do not migrate
current local entries automatically.

## Repositories

- Solo desktop app:
  `/Users/sachin/Developer/Orbit_Main/solo`
- AWS Vault backend:
  `/Users/sachin/Developer/Orbit_Main/solo-vault-backend`
- AWS Vault backend GitHub:
  `https://github.com/Sachin1801/solo-vault-backend`

## Current Local Vault Implementation

The local Vault implementation currently lives in the Solo desktop app repo.

Important files:

- `crates/solo-vault/src/pipeline.rs`
- `crates/solo-vault/src/store.rs`
- `apps/desktop/src-tauri/src/vault_commands.rs`
- `apps/desktop/src/lib/tauri/vault.ts`
- `apps/desktop/src/stores/vaultStore.ts`
- `apps/desktop/src/hooks/useVaultStream.ts`

Local storage locations:

- SQLite DB: `~/.solo/vault/index.sqlite`
- Blob files: `~/.solo/vault/blobs`
- Local model cache: `~/.solo/vault/models`

Local indexing flow today:

1. User drops a file into the Vault UI.
2. Frontend calls `vaultDropPaths(...)`.
3. Tauri command `vault_drop_paths` opens the singleton local `Vault`.
4. `Vault::drop_paths` calls `pipeline::ingest_and_store`.
5. The pipeline validates the file, currently max 50 MB.
6. It classifies file kind/subkind from extension and MIME.
7. It copies the file into `~/.solo/vault/blobs`.
8. It extracts text locally.
9. It chunks text at about 500 words with 50-word overlap.
10. It writes metadata into `entries`.
11. It writes chunks into `chunks` and `chunks_fts`.
12. It best-effort embeds chunks locally.
13. The backend emits `backend-event` events and the React store updates.

Current local search:

- FTS5 lexical search through `chunks_fts`.
- Local semantic search over stored embedding BLOBs.
- Local semantic model today is MiniLM, 384 dimensions.
- If semantic search cannot run, it falls back to FTS.

Current local DB tables:

- `entries`
- `chunks`
- `chunks_fts`

## Product Decisions For This Implementation

- Use current Cognito Hosted UI auth.
- Use the Cognito JWT `sub` as the canonical `user_id`.
- Do not implement the older Supabase-to-Cognito `/auth/link` bridge for this
  v1 integration.
- Do not migrate existing local SQLite entries for v1.
- New Vault uploads should go to AWS.
- Existing local entries should remain visible/searchable as offline entries.
- Use BGE-M3 for embeddings in AWS.
- BGE-M3 dense embeddings are 1024 dimensions, so PostgreSQL pgvector columns
  should use `vector(1024)`.
- Reference: `https://huggingface.co/BAAI/bge-m3`

## Current AWS Backend State

The AWS Vault backend repo currently has:

- CloudFormation bootstrap templates.
- RDS schema.
- Secrets/KMS baseline.
- API Gateway resource definitions.
- A DB migration Lambda.
- Documentation and work breakdown.

Important backend files:

- `README.md`
- `docs/API.md`
- `docs/WORK_BREAKDOWN.md`
- `db/schema.sql`
- `deploy.md`
- `infra/cloudformation/api-gateway.yml`
- `infra/cloudformation/rds.yml`
- `infra/cloudformation/secrets.yml`
- `infra/cloudformation/shared-network.yml`
- `infra/lambda/db-migrate/handler.ts`

Important gap:

- API Gateway routes currently return MOCK `501 NOT_IMPLEMENTED` responses.
- Real Lambda handlers for Vault CRUD, upload, search, WebSocket progress, and
  the indexing pipeline still need to be implemented.

## AWS Architecture To Build

Required AWS services:

- API Gateway REST API
- API Gateway WebSocket API
- Lambda
- S3
- SQS
- Step Functions
- EventBridge
- SNS
- RDS PostgreSQL with pgvector
- Secrets Manager
- KMS
- CloudWatch
- ECR, if BGE-M3 is packaged into a Lambda container image

High-level flow:

1. Solo creates a Vault entry through the REST API.
2. Backend stores metadata in RDS with `index_status = pending`.
3. Solo requests an S3 pre-signed upload URL.
4. Solo uploads the file directly to S3.
5. S3 ObjectCreated sends an event to SQS.
6. SQS triggers the Step Functions indexing pipeline.
7. Pipeline validates, extracts, chunks, embeds, stores, and notifies.
8. EventBridge/SNS/WebSocket sends indexing progress back to Solo.
9. Solo updates the Vault UI from progress events.

## REST API Routes

Implement these routes behind real Lambda integrations:

- `GET /vault/entries`
- `POST /vault/entries`
- `GET /vault/entries/{id}`
- `PUT /vault/entries/{id}`
- `DELETE /vault/entries/{id}`
- `POST /vault/entries/{id}/upload`
- `GET /vault/entries/{id}/download`
- `POST /vault/search`

All routes except unauthenticated health checks must require:

```http
Authorization: Bearer <cognito_access_token>
```

The backend must derive `user_id` from the JWT claims. Never trust a
client-supplied `user_id`.

## S3 Key Structure

Use per-user and per-project object paths:

```text
users/{user_id}/global/files/{entry_id}/{filename}
users/{user_id}/projects/{project_id}/files/{entry_id}/{filename}
```

Rules:

- Global entries have no `project_id`.
- Project entries include `project_id`.
- The upload Lambda should generate the key and persist it on the entry.
- The client should not choose arbitrary S3 keys.

## RDS Schema Direction

Use PostgreSQL with pgvector.

Core tables:

- `users`
- `projects`
- `vault_entries`
- `vault_chunk_parents`
- `vault_chunks`

Schema requirements:

- `vault_entries.user_id` is the Cognito `sub`.
- `vault_entries.project_id = NULL` means global/user-wide.
- `vault_entries.project_id = <id>` means project-scoped.
- `vault_entries.s3_key` stores the S3 object key.
- `vault_entries.index_status` tracks `pending`, `indexing`, `indexed`, or
  `failed`.
- `vault_chunks.embedding` should be `vector(1024)` for BGE-M3.
- Add a dense pgvector index for cosine search.
- Keep a sparse `tsvector` index for hybrid keyword search.

Conceptual mapping from local SQLite:

| Local SQLite | AWS RDS |
| --- | --- |
| `entries.id` | `vault_entries.id` |
| `entries.kind` / `entries.subkind` | `vault_entries.entry_type` plus metadata |
| `entries.scope_type = global` | `vault_entries.project_id = NULL` |
| `entries.scope_type = project` | `vault_entries.project_id = project_id` |
| `entries.vault_blob_path` | `vault_entries.s3_key` |
| `entries.tags` JSON string | `vault_entries.tags` text array |
| `entries.index_status` | `vault_entries.index_status` |
| `chunks.content` | `vault_chunks.content` |
| `chunks.token_count` | `vault_chunks.token_count` |
| `chunks.embedding` 384-dim local BLOB | `vault_chunks.embedding vector(1024)` |
| `chunks_fts` | PostgreSQL `tsvector` GIN index |

Do not copy local embedding vectors to AWS because the local model is 384-dim
MiniLM and the AWS service will use 1024-dim BGE-M3.

## Indexing Pipeline

Step Functions stages:

1. Validate
2. Extract
3. Chunk
4. Embed
5. Store
6. Notify

### Validate

Input:

- `entry_id`
- `user_id`
- `project_id`
- `s3_key`
- `file_name`
- `mime_type`
- `file_size`

Checks:

- Entry exists.
- Entry belongs to the authenticated user.
- S3 key matches the expected user/project path.
- File size is within the configured limit.
- MIME/type is supported.
- User quota is not exceeded, if quotas are added.

On success:

- Set `index_status = indexing`.
- Emit progress.

On failure:

- Set `index_status = failed`.
- Emit failed progress.
- Send to DLQ after configured retries.

### Extract

Extract text from supported files:

- PDF
- Markdown
- Plain text
- JSON/YAML/TOML
- Code files
- CSV
- DOCX if feasible

For unsupported binary files:

- Do not crash the pipeline.
- Mark extraction failed or store metadata-only status.
- Notify the client.

### Chunk

Use the same initial chunking policy as local:

- About 500 words per child chunk.
- About 50 words overlap.

If implementing parent/child chunks:

- Parent chunks are larger context windows.
- Child chunks are what get embedded and matched.
- Search returns the child match plus parent context.

### Embed

Use BGE-M3.

Implementation recommendation:

- Package BGE-M3 into an ECR Lambda container image.
- Load the model once per warm container.
- Use enough memory and ephemeral storage for the 1.5 GB model.
- Batch chunks conservatively to avoid timeouts.
- Output 1024-dim dense vectors.

If Lambda cold starts or model size become too painful:

- Keep the same SQS/Step Functions contract.
- Move only the embed stage to ECS/Fargate.
- Do not change the REST API or database contract.

### Store

Store chunks and embeddings in a transaction:

- Delete previous chunks for the entry if re-indexing.
- Insert parent chunks.
- Insert child chunks.
- Insert `vector(1024)` embeddings.
- Update `vault_entries.index_status = indexed`.

### Notify

Emit progress through EventBridge/SNS/WebSocket.

Progress event shape should be compatible with Solo's current Vault stream
concept:

```json
{
  "type": "index_progress",
  "entry_id": "uuid",
  "step": "embed",
  "step_index": 4,
  "total_steps": 6,
  "status": "running",
  "message": "Generating embeddings..."
}
```

## Solo Desktop Changes

Add config:

- `SOLO_VAULT_API_ENDPOINT`
- `SOLO_VAULT_WS_ENDPOINT`

Keep existing config:

- `SOLO_API_ENDPOINT`

Reason:

- `SOLO_API_ENDPOINT` already points to the main Solo API for stats/GitHub.
- Vault should be able to point at the separate `solo-vault-backend` service.

Add a remote Vault client:

- Use existing `auth_get_access_token` to get the Cognito access token.
- Send `Authorization: Bearer <token>`.
- Use REST for CRUD/upload/search.
- Use WebSocket for index progress.

New upload flow in Solo:

1. User drops a file.
2. Solo calls remote `POST /vault/entries`.
3. Solo calls remote `POST /vault/entries/{id}/upload`.
4. Solo PUTs the file bytes to the returned S3 pre-signed URL.
5. Solo marks entry as uploading/indexing.
6. WebSocket updates entry state to indexed or failed.

List behavior:

- `vault_list` should merge:
  - remote AWS entries
  - existing local offline SQLite entries

Search behavior:

- Query remote `/vault/search` first.
- Optionally append local offline search results for entries that are not in AWS.
- Clearly keep local-only entries as `cloud_sync_state = offline`.

Mutation behavior:

- Remote entries route to AWS APIs.
- Local-only entries route to existing SQLite methods.
- The easiest discriminator is whether the entry exists remotely or has a
  non-offline cloud sync state.

Delete behavior:

- Remote delete removes RDS entry, chunks, and S3 object.
- Local delete keeps existing behavior and removes local blob/chunks.

## Security Rules

- Every remote Vault API call must require Cognito JWT auth.
- Backend derives `user_id` from JWT claims.
- Backend must check ownership on every entry, file, and search query.
- S3 upload URLs must be scoped to the caller's expected object key.
- S3 objects should use KMS encryption.
- RDS should remain private.
- Lambda DB access should use Secrets Manager credentials.
- Do not return raw S3 keys for users other than the caller.

## Deployment Steps

1. Deploy or reuse the shared network stack.
2. Deploy KMS and Secrets Manager stack.
3. Deploy RDS PostgreSQL with pgvector support.
4. Apply `db/schema.sql` after updating embedding dimensions to `vector(1024)`.
5. Deploy S3 bucket and upload event notification.
6. Deploy SQS queue and DLQ.
7. Deploy Step Functions state machine.
8. Deploy Lambda handlers and BGE-M3 embed container.
9. Replace API Gateway MOCK integrations with real Lambda integrations.
10. Deploy WebSocket API and notification path.
11. Add `SOLO_VAULT_API_ENDPOINT` and `SOLO_VAULT_WS_ENDPOINT` to Solo desktop env.
12. Wire Solo desktop remote client and verify end to end.

## Test Plan

Backend auth:

- Missing JWT returns 401.
- Invalid JWT returns 401.
- User A cannot list/get/update/delete/search User B entries.

Backend upload:

- Create entry returns an entry owned by the current JWT `sub`.
- Upload URL points to the expected S3 key.
- Direct S3 PUT succeeds.
- S3 ObjectCreated starts the pipeline.

Pipeline:

- PDF indexes successfully.
- Markdown indexes successfully.
- Unsupported binary file does not crash the state machine.
- Failed extraction marks entry failed and notifies the client.
- Successful indexing writes RDS chunks and `vector(1024)` embeddings.

Search:

- Global entries appear for all projects owned by the same user.
- Project entries appear only for the matching project.
- Search does not return entries from another user.
- BGE-M3 semantic search returns relevant chunks.

Desktop:

- Signed-in file drop creates a remote entry.
- Upload progress/index progress appears in the UI.
- Existing local SQLite entries remain visible.
- Existing local SQLite entries are not uploaded automatically.
- Remote search works when online.
- Local-only entries remain searchable locally.

End-to-end demo:

1. Sign in to Solo.
2. Drop a PDF or Markdown file into Vault.
3. Confirm S3 object exists.
4. Confirm Step Functions run succeeds.
5. Confirm RDS has entry/chunk/vector rows.
6. Confirm WebSocket progress appears in Solo.
7. Search for content from the uploaded file.
8. Confirm Solo shows the indexed result.

## Acceptance Criteria

- New Vault uploads are stored in S3, not only local blobs.
- New Vault metadata and chunks are stored in RDS.
- BGE-M3 embeddings are stored in pgvector as `vector(1024)`.
- Search works through the AWS backend.
- Existing local entries remain usable and are not silently migrated.
- User data is isolated by Cognito `sub`.
- AWS pipeline progress is visible in the Solo Vault UI.

## Assumptions

- v1 does not migrate existing local SQLite rows.
- Cognito is the only auth path for this implementation.
- BGE-M3 is self-hosted for embedding.
- Backend repo owns AWS infra and Lambda code.
- Solo desktop only consumes the backend through REST and WebSocket APIs.
