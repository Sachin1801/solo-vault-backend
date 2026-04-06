# Work Breakdown — Solo Vault Backend

**Timeline:** 3 weeks (2026-04-06 to 2026-04-25)
**Team:** 3 backend/infra engineers

---

## Team Roles

| Role | Engineer | Focus Area | AWS Services Owned |
|------|----------|------------|-------------------|
| **Engineer 1** | _TBD_ | Infrastructure & IaC | Cognito, API Gateway, S3, RDS, KMS, Secrets Manager, CloudFront |
| **Engineer 2** | _TBD_ | Lambda Functions & APIs | Auth Lambda, CRUD Lambdas, File Lambdas, WebSocket Lambda |
| **Engineer 3** | _TBD_ | Pipeline & Data | Step Functions, SQS, EventBridge, SNS, DynamoDB, Search Lambda |

---

## Week 1: Foundation (Apr 6–12)

### Engineer 1 — Infrastructure

- [ ] **[INFRA-1] Set up IaC project** — Initialize AWS CDK (or CloudFormation/SAM) project structure. Set up dev/staging environments. Configure deployment scripts.
- [ ] **[INFRA-2] Cognito User Pool** — Create User Pool with custom auth flow (no hosted UI). Configure app client for server-side `AdminInitiateAuth`. Set up user attributes (email, sub).
- [ ] **[INFRA-3] API Gateway (REST)** — Create REST API with Cognito authorizer. Define all 14 resource paths with request/response models. Enable CORS for desktop app. Set up rate limiting (1000 req/min per user).
- [ ] **[INFRA-4] S3 Bucket** — Create vault bucket with per-user path structure. Enable S3 event notifications (ObjectCreated). Configure KMS server-side encryption. Set up lifecycle rules for cost management.
- [ ] **[INFRA-5] RDS PostgreSQL** — Provision RDS instance with pgvector extension. Run database migration (create tables: users, projects, vault_entries, vault_chunks). Configure security group for Lambda access. Set up Secrets Manager for DB credentials.
- [ ] **[INFRA-6] KMS + Secrets Manager** — Create KMS key for S3/RDS encryption. Store DB credentials, API keys in Secrets Manager. Configure Lambda IAM roles for secret access.

### Engineer 2 — Auth & CRUD Lambdas

- [ ] **[API-1] Auth Lambda (`auth-handler`)** — `POST /auth/link`: Verify Supabase JWT, create/find Cognito user via `AdminCreateUser`, return Cognito tokens via `AdminInitiateAuth`. `POST /auth/refresh`: Refresh Cognito tokens.
- [ ] **[API-2] Vault CRUD Lambda (`vault-crud`)** — `GET /vault/entries`: List with pagination, filter by project_id, scope, tags. `POST /vault/entries`: Create entry (validate type, store in RDS). `GET /vault/entries/{id}`: Get single entry. `PUT /vault/entries/{id}`: Update entry fields. `DELETE /vault/entries/{id}`: Delete entry + trigger S3 cleanup.
- [ ] **[API-3] Vault Files Lambda (`vault-files`)** — `POST /vault/entries/{id}/upload`: Generate pre-signed S3 upload URL (scoped to user's path). `GET /vault/entries/{id}/download`: Generate CloudFront signed URL for retrieval.

### Engineer 3 — Queue & Pipeline Skeleton

- [ ] **[PIPE-1] SQS Queue** — Create `vault-index-queue` with 5-minute visibility timeout. Configure dead-letter queue (`vault-index-dlq`, max receives: 3). Set up S3 → SQS event notification for new file uploads.
- [ ] **[PIPE-2] Step Functions State Machine** — Define 6-stage pipeline in ASL (Amazon States Language). Configure retry logic per stage (3 retries, exponential backoff). Set up error handling (catch → mark entry as `failed`). Wire SQS as trigger.
- [ ] **[PIPE-3] Validate Lambda (`pipeline-validate`)** — Check file type against allowed list (pdf, md, txt, json, yaml, py, js, ts, rs, go, etc.). Validate file size (max 50MB). Check user quota (max 1000 entries, 5GB total).

---

## Week 2: Pipeline + Integration (Apr 13–19)

### Engineer 1 — CDN & WebSocket

- [ ] **[INFRA-7] CloudFront Distribution** — Create distribution pointing to S3 vault bucket. Configure signed URLs (key pair + Lambda signer). Set cache TTLs for vault files. Restrict direct S3 access (OAI/OAC).
- [ ] **[INFRA-8] API Gateway (WebSocket)** — Create WebSocket API with `$connect` (Cognito auth via query param), `$disconnect`, and `index_progress` routes. Create DynamoDB table for connection mappings (`ws-connections`). Set up Lambda integrations.
- [ ] **[INFRA-9] CloudWatch Alarms** — Create alarms: Lambda error rate > 5%, Step Functions failures, SQS DLQ messages > 0, RDS CPU > 80%. Create dashboard with key metrics. Configure log groups with 30-day retention.

### Engineer 2 — WebSocket & DynamoDB

- [ ] **[API-4] WebSocket Lambda (`ws-notify`)** — Handle `$connect`: validate Cognito token, store connection ID + user_id in DynamoDB. Handle `$disconnect`: remove connection mapping. Handle SNS trigger: look up user's connections, push `index_progress` message via API Gateway Management API.
- [ ] **[API-5] DynamoDB Sessions Table** — Create `solo-sessions` table (PK: user_id, SK: session_id). Add GSI `project-sessions-index` (PK: project_id, SK: updated_at). Configure TTL on `ttl` attribute (30-day expiry). Set up on-demand capacity.
- [ ] **[API-6] Session Sync Lambdas** — `GET /sessions`: List user's cloud sessions (paginated). `POST /sessions/sync`: Upsert session data (messages, model, project_id). `GET /sessions/{id}`: Pull specific session. `DELETE /sessions/{id}`: Delete session. All scoped by Cognito user_id.

### Engineer 3 — Pipeline Completion

- [ ] **[PIPE-4] Extract Lambda (`pipeline-process:extract`)** — PDF text extraction (use `pdf-parse` or `@aws-sdk/textract`). Markdown/text: pass through. Code files: pass through with language tag. Images: OCR via Textract (if time) or skip with placeholder.
- [ ] **[PIPE-5] Chunk Lambda (`pipeline-process:chunk`)** — Split extracted text into ~500-token chunks with 50-token overlap. Preserve paragraph boundaries where possible. Output: array of `{ chunk_index, content, token_count }`.
- [ ] **[PIPE-6] Embed Lambda (`pipeline-process:embed`)** — Generate 1536-dimensional embeddings for each chunk. Use OpenAI `text-embedding-ada-002` or Amazon Bedrock Titan Embeddings (preferred — adds another AWS service). Batch API calls for efficiency.
- [ ] **[PIPE-7] Store Lambda (`pipeline-store`)** — Batch insert chunks + vectors into `vault_chunks` table. Update `vault_entries.index_status` to `indexed`. Use database transaction for atomicity.
- [ ] **[PIPE-8] EventBridge + SNS Notification** — Create EventBridge rule for Step Functions state changes. Route to SNS topic `vault-index-notifications`. SNS triggers `ws-notify` Lambda for WebSocket push. Include entry_id, step name, status in event payload.

---

## Week 3: Polish + Demo (Apr 20–25)

### All Engineers

- [ ] **[TEST-1] End-to-end testing** — Test complete flow: auth → create entry → upload file → indexing pipeline → search → session sync. Document test cases and results.
- [ ] **[TEST-2] Error handling audit** — Verify all Lambda error paths: invalid input, auth failures, S3 errors, RDS connection issues, pipeline failures. Ensure DLQ catches failed jobs. Verify CloudWatch alarms fire.
- [ ] **[TEST-3] Performance testing** — Test with 50+ vault entries. Measure: search latency (target < 500ms), upload-to-indexed latency, WebSocket delivery time. Tune pgvector index parameters if needed.
- [ ] **[OPS-1] Demo environment** — Set up clean demo account with sample data. Pre-index example entries (PDF, notes, code snippets). Prepare Step Functions console view for live demo. Ensure CloudWatch dashboard is presentable.
- [ ] **[OPS-2] Documentation** — API documentation with request/response examples. Architecture diagram. Deployment guide. Cost estimate for running the stack.

---

## API Specification

### Authentication

```
POST /auth/link
  Request:  { "supabase_token": "string", "email": "string" }
  Response: { "access_token": "string", "refresh_token": "string", "id_token": "string", "expires_in": 3600 }

POST /auth/refresh
  Request:  { "refresh_token": "string" }
  Response: { "access_token": "string", "id_token": "string", "expires_in": 3600 }
```

### Vault CRUD

```
GET /vault/entries?project_id=uuid&scope=project|global&tags=tag1,tag2&page=1&limit=20
  Response: { "entries": [VaultEntry], "total": number, "page": number }

POST /vault/entries
  Request:  { "title": "string", "content": "string?", "entry_type": "note|file|snippet|config|keyvalue", "tags": ["string"], "metadata": {}, "project_id": "uuid?" }
  Response: VaultEntry

GET /vault/entries/{id}
  Response: VaultEntry (with full content)

PUT /vault/entries/{id}
  Request:  { "title?": "string", "content?": "string", "tags?": ["string"], "metadata?": {} }
  Response: VaultEntry

DELETE /vault/entries/{id}
  Response: { "deleted": true }
```

### Vault Files

```
POST /vault/entries/{id}/upload
  Request:  { "file_name": "string", "mime_type": "string", "file_size": number }
  Response: { "upload_url": "string (pre-signed S3 URL)", "s3_key": "string" }

GET /vault/entries/{id}/download
  Response: { "download_url": "string (CloudFront signed URL)", "expires_in": 3600 }
```

### Vault Search

```
POST /vault/search
  Request:  { "query": "string", "project_id": "uuid?", "top_k": 5, "threshold": 0.7 }
  Response: { "results": [{ "entry_id": "uuid", "title": "string", "chunk_content": "string", "similarity": 0.89 }] }
```

### Session Sync

```
GET /sessions?project_id=uuid&page=1&limit=20
  Response: { "sessions": [SessionSummary], "total": number }

POST /sessions/sync
  Request:  { "session_id": "string", "messages": [...], "model": "string", "project_id": "uuid?" }
  Response: { "synced": true, "updated_at": "timestamp" }

GET /sessions/{id}
  Response: { "session_id": "string", "messages": [...], "model": "string", "project_id": "uuid?", "created_at": "timestamp" }

DELETE /sessions/{id}
  Response: { "deleted": true }
```

### WebSocket

```
WSS connect: wss://api-id.execute-api.region.amazonaws.com/prod?token=cognito_jwt

Server → Client messages:
{
  "type": "index_progress",
  "entry_id": "uuid",
  "step": "validate|extract|chunk|embed|store|notify",
  "step_index": 1-6,
  "total_steps": 6,
  "status": "running|completed|failed",
  "message": "Human-readable progress description"
}
```

### Data Types

```typescript
interface VaultEntry {
  id: string;           // UUID
  user_id: string;      // Cognito sub
  project_id: string | null;  // null = global
  title: string;
  content: string | null;
  entry_type: 'note' | 'file' | 'snippet' | 'config' | 'keyvalue';
  tags: string[];
  metadata: Record<string, any>;
  s3_key: string | null;
  file_name: string | null;
  file_size: number | null;
  mime_type: string | null;
  index_status: 'pending' | 'indexing' | 'indexed' | 'failed';
  created_at: string;   // ISO 8601
  updated_at: string;
}

interface SessionSummary {
  session_id: string;
  project_id: string | null;
  model: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}
```

---

## Database Schema

```sql
-- Run on RDS PostgreSQL with pgvector extension

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE users (
  id UUID PRIMARY KEY,
  email TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE projects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id),
  name TEXT NOT NULL,
  workspace_path TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE vault_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id),
  project_id UUID REFERENCES projects(id) NULL,
  title TEXT NOT NULL,
  content TEXT,
  entry_type TEXT NOT NULL,
  tags TEXT[],
  metadata JSONB DEFAULT '{}',
  s3_key TEXT,
  file_name TEXT,
  file_size BIGINT,
  mime_type TEXT,
  index_status TEXT DEFAULT 'pending',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE vault_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_id UUID REFERENCES vault_entries(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  content TEXT NOT NULL,
  embedding vector(1536),
  token_count INT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_chunks_embedding ON vault_chunks
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_entries_user_project ON vault_entries(user_id, project_id);
CREATE INDEX idx_entries_tags ON vault_entries USING gin(tags);
```

---

## S3 Bucket Structure

```
solo-vault-{env}/
  users/
    {cognito_user_id}/
      global/
        files/{uuid}.{ext}
      projects/
        {project_id}/
          files/{uuid}.{ext}
```

---

## DynamoDB Tables

### solo-sessions
```
PK: user_id (String)
SK: session_id (String)
Attributes: messages (List), model (String), project_id (String), created_at (Number), updated_at (Number), ttl (Number)
GSI: project-sessions-index (PK: project_id, SK: updated_at)
```

### ws-connections
```
PK: connection_id (String)
Attributes: user_id (String), connected_at (Number), ttl (Number)
GSI: user-connections-index (PK: user_id)
```
