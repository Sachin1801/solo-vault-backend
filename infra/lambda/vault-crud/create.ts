import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import { created } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import {
  CLOUD_SYNC_STATES,
  ENTRY_KINDS,
  INDEX_STATUSES,
  MEMORY_TYPES,
  SCOPE_TYPES,
  type VaultEntry
} from "../shared/types.js";

// tags is stored as a JSON-string in vault.entries (local-parity). Validate
// that it parses to a string[] but keep storing as text — pgsql casts on read
// where filtering is needed (`tags::jsonb @> $N`).
const tagsJsonString = z.string().refine((value) => {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) && parsed.every((t) => typeof t === "string");
  } catch {
    return false;
  }
}, "tags must be a JSON-encoded array of strings");

const CreateRequest = z
  .object({
    id: z.string().min(1),
    kind: z.enum(ENTRY_KINDS),
    subkind: z.string().nullable().optional(),
    title: z.string().min(1).max(500),
    content: z.string().nullable().optional(),
    source_path: z.string().nullable().optional(),
    vault_blob_path: z.string().nullable().optional(),
    scope_type: z.enum(SCOPE_TYPES),
    scope_project_id: z.string().nullable().optional(),
    memory_type: z.enum(MEMORY_TYPES),
    pinned: z.union([z.literal(0), z.literal(1)]).default(0),
    tags: tagsJsonString.default("[]"),
    mime: z.string().nullable().optional(),
    size_bytes: z.number().int().nonnegative().nullable().optional(),
    index_status: z.enum(INDEX_STATUSES),
    cloud_sync_state: z.enum(CLOUD_SYNC_STATES),
    classifier_confidence: z.number().min(0).max(1),
    hit_count: z.number().int().nonnegative().default(0),
    last_retrieved_at: z.number().int().nullable().optional(),
    created_at: z.number().int(),
    updated_at: z.number().int()
  })
  .refine(
    (data) =>
      data.scope_type === "project" ? !!data.scope_project_id : true,
    { message: "scope_project_id required when scope_type='project'", path: ["scope_project_id"] }
  )
  .refine(
    (data) =>
      data.scope_type === "global" ? !data.scope_project_id : true,
    { message: "scope_project_id must be omitted when scope_type='global'", path: ["scope_project_id"] }
  );

export async function createEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  let body: unknown;
  try {
    body = event.body ? JSON.parse(event.body) : {};
  } catch {
    throw ApiError.invalidInput("Request body is not valid JSON");
  }
  const parsed = CreateRequest.safeParse(body);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const path = issue?.path?.join(".") ?? "(root)";
    throw ApiError.invalidInput(`${path}: ${issue?.message ?? "Invalid body"}`);
  }
  const input = parsed.data;

  const rows = await query<VaultEntry>(
    `INSERT INTO vault.entries (
       id, user_id, kind, subkind, title, content,
       source_path, vault_blob_path,
       scope_type, scope_project_id, memory_type,
       pinned, tags, mime, size_bytes,
       index_status, cloud_sync_state, classifier_confidence,
       hit_count, last_retrieved_at,
       created_at, updated_at
     ) VALUES (
       $1, $2, $3, $4, $5, $6,
       $7, $8,
       $9, $10, $11,
       $12, $13, $14, $15,
       $16, $17, $18,
       $19, $20,
       $21, $22
     )
     RETURNING *`,
    [
      input.id,
      auth.user_id,
      input.kind,
      input.subkind ?? null,
      input.title,
      input.content ?? null,
      input.source_path ?? null,
      input.vault_blob_path ?? null,
      input.scope_type,
      input.scope_project_id ?? null,
      input.memory_type,
      input.pinned,
      input.tags,
      input.mime ?? null,
      input.size_bytes ?? null,
      input.index_status,
      input.cloud_sync_state,
      input.classifier_confidence,
      input.hit_count,
      input.last_retrieved_at ?? null,
      input.created_at,
      input.updated_at
    ]
  );

  return created(rows[0]);
}
