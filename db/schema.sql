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
-- Vault is empty in dev — destructive DROP CASCADE is safe.

-- Drop legacy schema (INFRA-5 v1)
DROP TABLE IF EXISTS vault_chunks CASCADE;
DROP TABLE IF EXISTS vault_chunk_parents CASCADE;
DROP TABLE IF EXISTS vault_entries CASCADE;
DROP TABLE IF EXISTS projects CASCADE;
DROP TABLE IF EXISTS users CASCADE;

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
    updated_at              bigint NOT NULL
);

-- user_id-prefixed indexes for hot paths (per-user list/scope/kind/pin).
CREATE INDEX idx_entries_user            ON vault.entries(user_id);
CREATE INDEX idx_entries_user_scope      ON vault.entries(user_id, scope_type, scope_project_id);
CREATE INDEX idx_entries_user_kind       ON vault.entries(user_id, kind);
CREATE INDEX idx_entries_user_pin        ON vault.entries(user_id, pinned);

CREATE TABLE IF NOT EXISTS vault.chunks (
    id           text PRIMARY KEY,
    entry_id     text NOT NULL REFERENCES vault.entries(id) ON DELETE CASCADE,
    user_id      text NOT NULL,                      -- cloud-only, mirrors entries.user_id
    chunk_index  bigint NOT NULL,
    content      text NOT NULL,
    token_count  bigint,
    embedding    vector(384),                        -- MiniLM dimension; matches local
    UNIQUE (entry_id, chunk_index)
);

CREATE INDEX idx_chunks_entry            ON vault.chunks(entry_id);
CREATE INDEX idx_chunks_user             ON vault.chunks(user_id);

-- pgvector cosine ANN. ivfflat is fine for <1M rows; revisit lists/index
-- choice once we have enough chunks to benchmark.
CREATE INDEX idx_chunks_embedding_cosine
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

CREATE INDEX idx_chunks_fts_entry        ON vault.chunks_fts(entry_id);
CREATE INDEX idx_chunks_fts_chunk        ON vault.chunks_fts(chunk_id);
CREATE INDEX idx_chunks_fts_user         ON vault.chunks_fts(user_id);
CREATE INDEX idx_chunks_fts_content_gin  ON vault.chunks_fts USING gin (to_tsvector('english', content));
