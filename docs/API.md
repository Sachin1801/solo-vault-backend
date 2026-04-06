# Solo Vault Backend — API Reference

Base URL: `https://{api-id}.execute-api.{region}.amazonaws.com/prod`
WebSocket: `wss://{ws-api-id}.execute-api.{region}.amazonaws.com/prod`

All REST endpoints require Cognito JWT in `Authorization: Bearer {token}` header.

---

## Authentication

### Link Supabase Account to Cognito

```http
POST /auth/link
Content-Type: application/json

{
  "supabase_token": "eyJhbGciOiJIUzI1NiIs...",
  "email": "user@example.com"
}
```

**Response (200):**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "refresh_token": "eyJjdHkiOiJKV1QiLC...",
  "id_token": "eyJhbGciOiJSUzI1NiIs...",
  "expires_in": 3600
}
```

### Refresh Tokens

```http
POST /auth/refresh
Content-Type: application/json

{
  "refresh_token": "eyJjdHkiOiJKV1QiLC..."
}
```

**Response (200):**
```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIs...",
  "id_token": "eyJhbGciOiJSUzI1NiIs...",
  "expires_in": 3600
}
```

---

## Vault Entries

### List Entries

```http
GET /vault/entries?project_id=uuid&scope=project&tags=api,design&page=1&limit=20
Authorization: Bearer {cognito_jwt}
```

**Query Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| project_id | UUID | No | Filter by project |
| scope | `project` \| `global` \| `all` | No | Filter by scope (default: `all`) |
| tags | string (comma-separated) | No | Filter by tags (AND logic) |
| page | number | No | Page number (default: 1) |
| limit | number | No | Items per page (default: 20, max: 100) |

**Response (200):**
```json
{
  "entries": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "user_id": "cognito-sub-uuid",
      "project_id": "project-uuid-or-null",
      "title": "API Design Notes",
      "content": "The REST API should follow...",
      "entry_type": "note",
      "tags": ["api", "design"],
      "metadata": {},
      "s3_key": null,
      "file_name": null,
      "file_size": null,
      "mime_type": null,
      "index_status": "indexed",
      "created_at": "2026-04-06T10:30:00Z",
      "updated_at": "2026-04-06T10:35:00Z"
    }
  ],
  "total": 42,
  "page": 1
}
```

### Create Entry

```http
POST /vault/entries
Authorization: Bearer {cognito_jwt}
Content-Type: application/json

{
  "title": "Database Schema Notes",
  "content": "We're using PostgreSQL with pgvector...",
  "entry_type": "note",
  "tags": ["database", "schema"],
  "metadata": { "priority": "high" },
  "project_id": "project-uuid-or-null"
}
```

**Response (201):** `VaultEntry` object

### Get Entry

```http
GET /vault/entries/{id}
Authorization: Bearer {cognito_jwt}
```

**Response (200):** `VaultEntry` object with full content

### Update Entry

```http
PUT /vault/entries/{id}
Authorization: Bearer {cognito_jwt}
Content-Type: application/json

{
  "title": "Updated Title",
  "tags": ["database", "schema", "pgvector"]
}
```

Only include fields you want to update. **Response (200):** Updated `VaultEntry`

### Delete Entry

```http
DELETE /vault/entries/{id}
Authorization: Bearer {cognito_jwt}
```

**Response (200):**
```json
{ "deleted": true }
```

Deleting an entry also removes its S3 file (if any) and all associated chunks.

---

## Vault Files

### Get Upload URL

```http
POST /vault/entries/{id}/upload
Authorization: Bearer {cognito_jwt}
Content-Type: application/json

{
  "file_name": "requirements.pdf",
  "mime_type": "application/pdf",
  "file_size": 1048576
}
```

**Response (200):**
```json
{
  "upload_url": "https://solo-vault-prod.s3.amazonaws.com/users/...",
  "s3_key": "users/cognito-sub/projects/proj-uuid/files/file-uuid.pdf"
}
```

**Then upload the file directly to S3:**
```http
PUT {upload_url}
Content-Type: application/pdf

<binary file data>
```

The S3 upload triggers the indexing pipeline automatically.

### Get Download URL

```http
GET /vault/entries/{id}/download
Authorization: Bearer {cognito_jwt}
```

**Response (200):**
```json
{
  "download_url": "https://d1234.cloudfront.net/users/...",
  "expires_in": 3600
}
```

---

## Vault Search

### Semantic Search

```http
POST /vault/search
Authorization: Bearer {cognito_jwt}
Content-Type: application/json

{
  "query": "How does the authentication flow work?",
  "project_id": "project-uuid-or-null",
  "top_k": 5,
  "threshold": 0.7
}
```

**Response (200):**
```json
{
  "results": [
    {
      "entry_id": "entry-uuid",
      "title": "Auth Flow Documentation",
      "chunk_content": "The authentication flow starts with Supabase OAuth...",
      "chunk_index": 2,
      "similarity": 0.92,
      "entry_type": "note",
      "tags": ["auth", "documentation"]
    },
    {
      "entry_id": "entry-uuid-2",
      "title": "API Design Notes",
      "chunk_content": "...the /auth/link endpoint verifies the Supabase token...",
      "chunk_index": 5,
      "similarity": 0.85,
      "entry_type": "note",
      "tags": ["api", "design"]
    }
  ]
}
```

When `project_id` is provided, searches both project-scoped AND global entries for that user.

---

## Session Sync

### List Sessions

```http
GET /sessions?project_id=uuid&page=1&limit=20
Authorization: Bearer {cognito_jwt}
```

**Response (200):**
```json
{
  "sessions": [
    {
      "session_id": "session-uuid",
      "project_id": "project-uuid",
      "model": "claude-sonnet-4-5-20250514",
      "message_count": 12,
      "created_at": "2026-04-06T10:00:00Z",
      "updated_at": "2026-04-06T11:30:00Z"
    }
  ],
  "total": 5
}
```

### Sync Session

```http
POST /sessions/sync
Authorization: Bearer {cognito_jwt}
Content-Type: application/json

{
  "session_id": "local-session-uuid",
  "messages": [ ... ],
  "model": "claude-sonnet-4-5-20250514",
  "project_id": "project-uuid-or-null"
}
```

**Response (200):**
```json
{ "synced": true, "updated_at": "2026-04-06T11:30:00Z" }
```

### Pull Session

```http
GET /sessions/{id}
Authorization: Bearer {cognito_jwt}
```

**Response (200):** Full session object with messages

### Delete Session

```http
DELETE /sessions/{id}
Authorization: Bearer {cognito_jwt}
```

**Response (200):**
```json
{ "deleted": true }
```

---

## WebSocket API

### Connect

```
wss://{ws-api-id}.execute-api.{region}.amazonaws.com/prod?token={cognito_jwt}
```

The `$connect` handler validates the Cognito token and stores the connection mapping.

### Server → Client Messages

**Indexing Progress:**
```json
{
  "type": "index_progress",
  "entry_id": "entry-uuid",
  "step": "embed",
  "step_index": 4,
  "total_steps": 6,
  "status": "running",
  "message": "Generating embeddings for 12 chunks..."
}
```

**Step values:** `validate`, `extract`, `chunk`, `embed`, `store`, `notify`
**Status values:** `running`, `completed`, `failed`

---

## Error Responses

All errors follow this format:

```json
{
  "error": {
    "code": "ENTRY_NOT_FOUND",
    "message": "Vault entry with id 'uuid' not found"
  }
}
```

| HTTP Status | Code | Description |
|-------------|------|-------------|
| 400 | `INVALID_INPUT` | Request body validation failed |
| 401 | `UNAUTHORIZED` | Missing or invalid Cognito JWT |
| 403 | `FORBIDDEN` | User doesn't own this resource |
| 404 | `ENTRY_NOT_FOUND` | Vault entry not found |
| 404 | `SESSION_NOT_FOUND` | Session not found |
| 409 | `QUOTA_EXCEEDED` | User hit entry or storage quota |
| 500 | `INTERNAL_ERROR` | Unexpected server error |
