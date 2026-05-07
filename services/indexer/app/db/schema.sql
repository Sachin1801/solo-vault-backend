CREATE SCHEMA IF NOT EXISTS vault;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS vault.entries (
    id                      text PRIMARY KEY,
    user_id                 text NOT NULL,
    kind                    text NOT NULL,
    subkind                 text,
    title                   text NOT NULL,
    content                 text,
    source_path             text,
    vault_blob_path         text,
    scope_type              text NOT NULL DEFAULT 'global',
    scope_project_id        text,
    memory_type             text NOT NULL DEFAULT 'user',
    pinned                  integer NOT NULL DEFAULT 0,
    tags                    text NOT NULL DEFAULT '[]',
    mime                    text,
    size_bytes              bigint,
    index_status            text NOT NULL DEFAULT 'pending',
    cloud_sync_state        text NOT NULL DEFAULT 'pending',
    classifier_confidence   double precision NOT NULL DEFAULT 1,
    hit_count               bigint NOT NULL DEFAULT 0,
    last_retrieved_at       bigint,
    created_at              bigint NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::bigint),
    updated_at              bigint NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::bigint),
    file_hash               text,
    chunk_count             bigint NOT NULL DEFAULT 0,
    embedding_model         text,
    chunker_version         text,
    index_error             text,
    uploaded_at             bigint,
    indexed_at              bigint,
    owner_user_id           text,
    organization_id         text,
    project_id              text
);

ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS file_hash text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS chunk_count bigint NOT NULL DEFAULT 0;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS embedding_model text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS chunker_version text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS index_error text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS uploaded_at bigint;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS indexed_at bigint;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS owner_user_id text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS organization_id text;
ALTER TABLE vault.entries ADD COLUMN IF NOT EXISTS project_id text;
UPDATE vault.entries SET owner_user_id = user_id WHERE owner_user_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_entries_user            ON vault.entries(user_id);
CREATE INDEX IF NOT EXISTS idx_entries_user_scope      ON vault.entries(user_id, scope_type, scope_project_id);
CREATE INDEX IF NOT EXISTS idx_entries_user_kind       ON vault.entries(user_id, kind);
CREATE INDEX IF NOT EXISTS idx_entries_user_pin        ON vault.entries(user_id, pinned);
CREATE INDEX IF NOT EXISTS idx_entries_user_hash       ON vault.entries(user_id, file_hash);
CREATE INDEX IF NOT EXISTS idx_entries_user_status     ON vault.entries(user_id, index_status);

CREATE TABLE IF NOT EXISTS vault.chunks (
    id           text PRIMARY KEY,
    entry_id     text NOT NULL REFERENCES vault.entries(id) ON DELETE CASCADE,
    user_id      text NOT NULL,
    chunk_index  bigint NOT NULL,
    content      text NOT NULL,
    token_count  bigint,
    embedding    vector(384),
    chunk_hash   text,
    UNIQUE (entry_id, chunk_index)
);

ALTER TABLE vault.chunks ADD COLUMN IF NOT EXISTS chunk_hash text;

CREATE INDEX IF NOT EXISTS idx_chunks_entry            ON vault.chunks(entry_id);
CREATE INDEX IF NOT EXISTS idx_chunks_user             ON vault.chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_user_hash        ON vault.chunks(user_id, chunk_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_cosine ON vault.chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE IF NOT EXISTS vault.chunks_fts (
    content  text NOT NULL,
    entry_id text NOT NULL,
    chunk_id text NOT NULL,
    user_id  text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_fts_entry        ON vault.chunks_fts(entry_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_chunk        ON vault.chunks_fts(chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_user         ON vault.chunks_fts(user_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts_content_gin  ON vault.chunks_fts USING gin (to_tsvector('english', content));
