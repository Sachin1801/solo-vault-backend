-- Solo Vault database schema
-- Target: PostgreSQL 15+ with pgvector
-- Source of truth: docs/WORK_BREAKDOWN.md (INFRA-5) + retrieval design (API-7)
--
-- The base four tables (users, projects, vault_entries, vault_chunks) come from
-- the work breakdown. The hierarchical-chunk columns (parent_id, context_prefix,
-- tsv) come from the retrieval plan: hybrid BM25+vector search, contextual
-- retrieval at index time, small-to-big parent hydration at query time.
-- See the PR description for the full design.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- Users / projects / entries
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY,
  email TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS projects (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id UUID REFERENCES users(id),
  name TEXT NOT NULL,
  workspace_path TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS vault_entries (
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

CREATE INDEX IF NOT EXISTS idx_entries_user_project
  ON vault_entries(user_id, project_id);
CREATE INDEX IF NOT EXISTS idx_entries_tags
  ON vault_entries USING gin(tags);

-- ---------------------------------------------------------------------------
-- Hierarchical chunks: parents (returned as context) + children (matched against)
-- ---------------------------------------------------------------------------
-- Parent chunks (~800 tokens, paragraph/section-aligned) are what we feed the
-- LLM after retrieval. Child chunks (~200 tokens) are what we embed and match.
-- Match precisely on small; ground broadly on parent. Each child FK's to its
-- parent for the JOIN at query time.

CREATE TABLE IF NOT EXISTS vault_chunk_parents (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_id UUID REFERENCES vault_entries(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,
  content TEXT NOT NULL,
  token_count INT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_parents_entry
  ON vault_chunk_parents(entry_id);

CREATE TABLE IF NOT EXISTS vault_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_id UUID REFERENCES vault_entries(id) ON DELETE CASCADE,
  parent_id UUID REFERENCES vault_chunk_parents(id) ON DELETE CASCADE,
  chunk_index INT NOT NULL,

  -- Raw chunk text (what the user would read).
  content TEXT NOT NULL,

  -- Anthropic-style contextual retrieval prefix: an LLM-generated sentence
  -- explaining where this chunk sits in its source document. Stored so we
  -- can reindex without regenerating. The embedding is computed over
  -- (context_prefix || '\n\n' || content), not content alone.
  context_prefix TEXT,

  -- Dense side of hybrid search. 1536-dim matches ada-002 / Titan v2 8k.
  embedding vector(1536),

  -- Sparse side of hybrid search. Generated column keeps it in sync with
  -- context_prefix + content. English config is fine for mixed code+prose;
  -- revisit if users want multilingual.
  tsv tsvector GENERATED ALWAYS AS (
    to_tsvector('english', coalesce(context_prefix, '') || ' ' || content)
  ) STORED,

  token_count INT,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Dense ANN index. IVFFlat with lists=100 is a reasonable default for <1M
-- rows; revisit once we have data (TEST-3). For cosine distance operator `<=>`
-- use vector_cosine_ops (normalized embeddings from Titan/OpenAI).
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
  ON vault_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Sparse index for BM25-style lookup.
CREATE INDEX IF NOT EXISTS idx_chunks_tsv
  ON vault_chunks USING gin(tsv);

-- Query-time joins.
CREATE INDEX IF NOT EXISTS idx_chunks_parent
  ON vault_chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_entry
  ON vault_chunks(entry_id);

-- ---------------------------------------------------------------------------
-- Forward-compat notes (NOT created here)
-- ---------------------------------------------------------------------------
-- For the LightRAG benchmark, future migrations will add:
--   entities (id, entry_id, name, type, embedding vector(1536))
--   entity_relationships (id, from_entity_id, to_entity_id, relation_type, source_entry_id)
-- No changes to the tables above are required to add them later — they hang off
-- vault_entries via entry_id.
