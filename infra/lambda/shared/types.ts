export type EntryType = "note" | "file" | "snippet" | "config" | "keyvalue";

export type IndexStatus = "pending" | "indexing" | "indexed" | "failed";

export interface VaultEntry {
  id: string;
  user_id: string;
  project_id: string | null;
  title: string;
  content: string | null;
  entry_type: EntryType;
  tags: string[];
  metadata: Record<string, unknown>;
  s3_key: string | null;
  file_name: string | null;
  file_size: number | null;
  mime_type: string | null;
  index_status: IndexStatus;
  created_at: string;
  updated_at: string;
}
