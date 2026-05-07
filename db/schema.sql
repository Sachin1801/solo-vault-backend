-- Solo Vault cloud schema — local-parity with desktop SQLite store
--
-- Source of truth: Vault Local Index Schema.md (root) + the desktop
-- crates/solo-vault/src/store.rs SCHEMA constant.
--
-- This replaces the inside-out v1 schema (vault_entries with entry_type,
-- hierarchical chunks, vector(1536), TIMESTAMPTZ) with a 1:1 mirror of
-- the local store. Cloud rows are sync targets — column names, types, and
-- serialized values match the desktop's local SQLite shape.
--
-- The single cloud-only addition is `user_id text NOT NULL` on entries,
-- chunks, and chunks_fts (sourced from Cognito JWT sub) for per-user IDOR
-- scoping. Local columns are unchanged.
--
-- This schema is intentionally additive/rerunnable. Do not drop existing
-- Vault data from this file; use explicit one-off migrations for destructive
-- development resets.

CREATE SCHEMA IF NOT EXISTS vault;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS vault.entries (
    id                      text PRIMARY KEY,
    user_id                 text NOT NULL,           -- cloud-only (Cognito sub)
    kind                    text NOT NULL,
    subkind                 text,
    title                   text NOT NULL,
    content                 text,
    source_path             text,
    vault_blob_path         text,
    scope_type              text NOT NULL,
    scope_project_id        text,
    memory_type             text NOT NULL,
    pinned                  integer NOT NULL DEFAULT 0,
    tags                    text NOT NULL DEFAULT '[]',
    mime                    text,
    size_bytes              bigint,
    index_status            text NOT NULL,
    cloud_sync_state        text NOT NULL,
    classifier_confidence   double precision NOT NULL,
    hit_count               bigint NOT NULL DEFAULT 0,
    last_retrieved_at       bigint,
    created_at              bigint NOT NULL,
    updated_at              bigint NOT NULL,
    file_hash               text,
    chunk_count             bigint NOT NULL DEFAULT 0,
    embedding_model         text,
    chunker_version         text,
    index_error             text,
    uploaded_at             bigint,
    indexed_at              bigint
);

ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS file_hash text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS chunk_count bigint NOT NULL DEFAULT 0;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS chunker_version text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS index_error text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS uploaded_at bigint;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS indexed_at bigint;

-- user_id-prefixed indexes for hot paths (per-user list/scope/kind/pin).
CREATE INDEX IF NOT EXISTS idx_entries_user            ON vault.entries(user_id);
CREATE INDEX IF NOT EXISTS idx_entries_user_scope      ON vault.entries(user_id, scope_type, scope_project_id);
CREATE INDEX IF NOT EXISTS idx_entries_user_kind       ON vault.entries(user_id, kind);
CREATE INDEX IF NOT EXISTS idx_entries_user_pin        ON vault.entries(user_id, pinned);
CREATE INDEX IF NOT EXISTS idx_entries_user_hash       ON vault.entries(user_id, file_hash);
CREATE INDEX IF NOT EXISTS idx_entries_user_status     ON vault.entries(user_id, index_status);

CREATE TABLE IF NOT EXISTS vault.chunks (
    id           text PRIMARY KEY,
    entry_id     text NOT NULL REFERENCES vault.entries(id) ON DELETE CASCADE,
    user_id      text NOT NULL,                      -- cloud-only, mirrors entries.user_id
    chunk_index  bigint NOT NULL,
    content      text NOT NULL,
    token_count  bigint,
    embedding    vector(384),                        -- MiniLM dimension; matches local
    chunk_hash   text,
    UNIQUE (entry_id, chunk_index)
);

ALTER TABLE vault.chunks ADD COLUMN IF NOT EXISTS chunk_hash text;

CREATE INDEX IF NOT EXISTS idx_chunks_entry            ON vault.chunks(entry_id);
CREATE INDEX IF NOT EXISTS idx_chunks_user             ON vault.chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_user_hash        ON vault.chunks(user_id, chunk_hash);

-- pgvector cosine ANN. ivfflat is fine for <1M rows; revisit lists/index
-- choice once we have enough chunks to benchmark.
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_cosine
    ON vault.chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Materialized FTS mirror of chunks (matches local chunks_fts virtual table).
-- No FK by design — local treats this as a derived/rebuildable table, so we
-- mirror that. Delete handlers must clean this table explicitly.
CREATE TABLE IF NOT EXISTS vault.chunks_fts (
    content  text NOT NULL,
    entry_id text NOT NULL,
    chunk_id text NOT NULL,
    user_id  text NOT NULL                           -- cloud-only
);

CREATE INDEX IF NOT EXISTS idx_chunks_fts_entry        ON vault.chunks_fts(entry_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_chunk        ON vault.chunks_fts(chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_user         ON vault.chunks_fts(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_content_gin  ON vault.chunks_fts USING gin (to_tsvector('english', content));

CREATE TABLE IF NOT EXISTS vault.organizations (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    created_at  bigint NOT NULL,
    updated_at  bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS vault.organization_members (
    organization_id text NOT NULL REFERENCES vault.organizations(id) ON DELETE CASCADE,
    user_id         text NOT NULL,
    role            text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at      bigint NOT NULL,
    updated_at      bigint NOT NULL,
    PRIMARY KEY (organization_id, user_id)
);

CREATE TABLE IF NOT EXISTS vault.projects (
    id              text PRIMARY KEY,
    organization_id text NOT NULL REFERENCES vault.organizations(id) ON DELETE CASCADE,
    name            text NOT NULL,
    created_at      bigint NOT NULL,
    updated_at      bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS vault.project_members (
    project_id text NOT NULL REFERENCES vault.projects(id) ON DELETE CASCADE,
    user_id    text NOT NULL,
    role       text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at bigint NOT NULL,
    updated_at bigint NOT NULL,
    PRIMARY KEY (project_id, user_id)
);

CREATE TABLE IF NOT EXISTS vault.entry_access (
    entry_id     text NOT NULL REFERENCES vault.entries(id) ON DELETE CASCADE,
    subject_type text NOT NULL CHECK (subject_type IN ('user', 'organization', 'project')),
    subject_id   text NOT NULL,
    role         text NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at   bigint NOT NULL,
    updated_at   bigint NOT NULL,
    PRIMARY KEY (entry_id, subject_type, subject_id)
);

ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS organization_id text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS project_id text;
UPDATE vault.entries SET owner_user_id = user_id WHERE owner_user_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_entries_owner          ON vault.entries(owner_user_id);
CREATE INDEX IF NOT EXISTS idx_entries_organization   ON vault.entries(organization_id);
CREATE INDEX IF NOT EXISTS idx_entries_project        ON vault.entries(project_id);
CREATE INDEX IF NOT EXISTS idx_org_members_user       ON vault.organization_members(user_id);
CREATE INDEX IF NOT EXISTS idx_project_members_user   ON vault.project_members(user_id);
CREATE INDEX IF NOT EXISTS idx_entry_access_subject   ON vault.entry_access(subject_type, subject_id);
