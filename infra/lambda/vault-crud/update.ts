import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import { ok } from "../shared/response.js";
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

const tagsJsonString = z.string().refine((value) => {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) && parsed.every((t) => typeof t === "string");
  } catch {
    return false;
  }
}, "tags must be a JSON-encoded array of strings");

// All fields optional except updated_at (the desktop is the source of truth
// for this column — client must declare what point-in-time state the row
// represents). id, user_id, created_at are immutable.
const UpdateRequest = z
  .object({
    kind: z.enum(ENTRY_KINDS).optional(),
    subkind: z.string().nullable().optional(),
    title: z.string().min(1).max(500).optional(),
    content: z.string().nullable().optional(),
    source_path: z.string().nullable().optional(),
    vault_blob_path: z.string().nullable().optional(),
    scope_type: z.enum(SCOPE_TYPES).optional(),
    scope_project_id: z.string().nullable().optional(),
    memory_type: z.enum(MEMORY_TYPES).optional(),
    pinned: z.union([z.literal(0), z.literal(1)]).optional(),
    tags: tagsJsonString.optional(),
    mime: z.string().nullable().optional(),
    size_bytes: z.number().int().nonnegative().nullable().optional(),
    index_status: z.enum(INDEX_STATUSES).optional(),
    cloud_sync_state: z.enum(CLOUD_SYNC_STATES).optional(),
    classifier_confidence: z.number().min(0).max(1).optional(),
    hit_count: z.number().int().nonnegative().optional(),
    last_retrieved_at: z.number().int().nullable().optional(),
    updated_at: z.number().int()
  })
  .refine((data) => Object.keys(data).length > 1, {
    message: "Must update at least one field besides updated_at"
  });

export async function updateEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const id = event.pathParameters?.id;
  if (!id) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  let body: unknown;
  try {
    body = event.body ? JSON.parse(event.body) : {};
  } catch {
    throw ApiError.invalidInput("Request body is not valid JSON");
  }
  const parsed = UpdateRequest.safeParse(body);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const path = issue?.path?.join(".") ?? "(root)";
    throw ApiError.invalidInput(`${path}: ${issue?.message ?? "Invalid body"}`);
  }
  const input = parsed.data;

  // Build SET clause dynamically — only include fields the client provided.
  // (Object.keys preserves insertion order; zod parse output keeps the keys
  // present in the input.)
  const sets: string[] = [];
  const params: unknown[] = [];
  let p = 1;
  for (const [key, value] of Object.entries(input)) {
    sets.push(`${key} = $${p++}`);
    params.push(value);
  }
  params.push(id, auth.user_id);

  const rows = await query<VaultEntry>(
    `UPDATE vault.entries
        SET ${sets.join(", ")}
      WHERE id = $${p++} AND user_id = $${p}
      RETURNING *`,
    params
  );

  if (rows.length === 0) {
    throw ApiError.entryNotFound(id);
  }
  return ok(rows[0]);
}
