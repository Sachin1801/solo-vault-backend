// Type definitions mirroring the local-parity vault.* schema (db/schema.sql).
// Enums are stored as TEXT in Postgres but constrained to these values
// application-side via zod. See "Vault Local Index Schema.md" for the contract.

export const ENTRY_KINDS = [
  "document",
  "code",
  "snippet",
  "image",
  "design",
  "data",
  "config",
  "web",
  "note",
  "keyvalue",
  "audio",
  "archive",
  "unsorted"
] as const;
export type EntryKind = (typeof ENTRY_KINDS)[number];

export const MEMORY_TYPES = ["project", "user", "pinned_source_of_truth"] as const;
export type MemoryType = (typeof MEMORY_TYPES)[number];

export const SCOPE_TYPES = ["global", "project"] as const;
export type ScopeType = (typeof SCOPE_TYPES)[number];

export const INDEX_STATUSES = [
  "pending",
  "extracting",
  "chunking",
  "embedding",
  "storing",
  "indexed",
  "extraction_failed",
  "failed"
] as const;
export type IndexStatus = (typeof INDEX_STATUSES)[number];

export const CLOUD_SYNC_STATES = [
  "offline",
  "pending",
  "uploading",
  "indexing_remote",
  "synced",
  "failed"
] as const;
export type CloudSyncState = (typeof CLOUD_SYNC_STATES)[number];

// Row shape returned by SELECTs on vault.entries. Mirrors the column order
// in db/schema.sql for easy cross-reference.
export interface VaultEntry {
  id: string;
  user_id: string;
  kind: EntryKind;
  subkind: string | null;
  title: string;
  content: string | null;
  source_path: string | null;
  vault_blob_path: string | null;
  scope_type: ScopeType;
  scope_project_id: string | null;
  memory_type: MemoryType;
  pinned: number; // 0 or 1
  tags: string; // JSON-string of string[]
  mime: string | null;
  size_bytes: number | null;
  index_status: IndexStatus;
  cloud_sync_state: CloudSyncState;
  classifier_confidence: number;
  hit_count: number;
  last_retrieved_at: number | null;
  created_at: number; // epoch seconds
  updated_at: number;
  file_hash: string | null;
  chunk_count: number;
  embedding_model: string | null;
  chunker_version: string | null;
  index_error: string | null;
  uploaded_at: number | null;
  indexed_at: number | null;
  owner_user_id: string | null;
  organization_id: string | null;
  project_id: string | null;
}
