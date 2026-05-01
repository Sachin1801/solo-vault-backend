import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import { created } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import type { VaultEntry } from "../shared/types.js";

const CreateRequest = z.object({
  title: z.string().min(1).max(500),
  content: z.string().optional(),
  entry_type: z.enum(["note", "file", "snippet", "config", "keyvalue"]),
  tags: z.array(z.string()).default([]),
  metadata: z.record(z.unknown()).default({}),
  project_id: z.string().uuid().nullable().optional()
});

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
    throw ApiError.invalidInput(
      parsed.error.issues[0]?.message ?? "Invalid body"
    );
  }
  const input = parsed.data;

  // index_status defaults to 'pending' per schema. File entries leave
  // s3_key null until vault-files (API-3) issues an upload URL.
  const rows = await query<VaultEntry>(
    `INSERT INTO vault_entries
       (user_id, project_id, title, content, entry_type, tags, metadata)
     VALUES ($1, $2, $3, $4, $5, $6, $7)
     RETURNING *`,
    [
      auth.user_id,
      input.project_id ?? null,
      input.title,
      input.content ?? null,
      input.entry_type,
      input.tags,
      input.metadata
    ]
  );

  return created(rows[0]);
}
