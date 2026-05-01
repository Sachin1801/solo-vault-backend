import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import { ok } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import type { VaultEntry } from "../shared/types.js";

const PathSchema = z.object({ id: z.string().uuid() });

const UpdateRequest = z
  .object({
    title: z.string().min(1).max(500).optional(),
    content: z.string().nullable().optional(),
    tags: z.array(z.string()).optional(),
    metadata: z.record(z.unknown()).optional()
  })
  .refine(
    (data) => Object.keys(data).length > 0,
    { message: "At least one field must be provided" }
  );

export async function updateEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const path = PathSchema.safeParse(event.pathParameters ?? {});
  if (!path.success) {
    throw ApiError.invalidInput("Path parameter 'id' must be a UUID");
  }
  const { id } = path.data;

  let body: unknown;
  try {
    body = event.body ? JSON.parse(event.body) : {};
  } catch {
    throw ApiError.invalidInput("Request body is not valid JSON");
  }
  const parsed = UpdateRequest.safeParse(body);
  if (!parsed.success) {
    throw ApiError.invalidInput(
      parsed.error.issues[0]?.message ?? "Invalid body"
    );
  }
  const input = parsed.data;

  // Build dynamic SET — only update fields the caller sent. updated_at is
  // bumped unconditionally. If content is being changed, reset the index
  // pipeline so chunks/embeddings get rebuilt.
  const sets: string[] = ["updated_at = now()"];
  const params: unknown[] = [];

  if (input.title !== undefined) {
    params.push(input.title);
    sets.push(`title = $${params.length}`);
  }
  if (input.content !== undefined) {
    params.push(input.content);
    sets.push(`content = $${params.length}`);
    sets.push(`index_status = 'pending'`);
  }
  if (input.tags !== undefined) {
    params.push(input.tags);
    sets.push(`tags = $${params.length}`);
  }
  if (input.metadata !== undefined) {
    params.push(input.metadata);
    sets.push(`metadata = $${params.length}`);
  }

  params.push(id);
  const idParam = `$${params.length}`;
  params.push(auth.user_id);
  const userParam = `$${params.length}`;

  const rows = await query<VaultEntry>(
    `UPDATE vault_entries
        SET ${sets.join(", ")}
      WHERE id = ${idParam} AND user_id = ${userParam}
     RETURNING *`,
    params
  );
  if (rows.length === 0) {
    throw ApiError.entryNotFound(id);
  }
  return ok(rows[0]);
}
