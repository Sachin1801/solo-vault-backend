CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS vault_entries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT NOT NULL,
  project_id TEXT,
  entry_id TEXT UNIQUE NOT NULL,
  title TEXT NOT NULL,
  file_name TEXT,
  file_size BIGINT,
  mime_type TEXT,
  s3_key TEXT,
  kind TEXT NOT NULL,
  subkind TEXT,
  index_status TEXT NOT NULL DEFAULT 'pending',
  chunk_count INT DEFAULT 0,
  embedding_model TEXT,
  chunker_version TEXT,
  file_hash TEXT,
  classifier_confidence FLOAT,
  pinned BOOLEAN DEFAULT FALSE,
  memory_type TEXT,
  tags TEXT[],
  metadata JSONB DEFAULT '{}',
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vault_chunks (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  entry_id TEXT NOT NULL REFERENCES vault_entries(entry_id) ON DELETE CASCADE,
  user_id TEXT NOT NULL,
  chunk_index INT NOT NULL,
  content TEXT NOT NULL,
  embedding vector(1024),
  token_count INT,
  chunk_hash TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE (entry_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_chunks_embedding
  ON vault_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_entries_user ON vault_entries(user_id);
CREATE INDEX IF NOT EXISTS idx_entries_status ON vault_entries(index_status);
CREATE INDEX IF NOT EXISTS idx_chunks_user ON vault_chunks(user_id);
