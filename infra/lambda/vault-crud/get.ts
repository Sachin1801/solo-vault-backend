import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import { ok } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import type { VaultEntry } from "../shared/types.js";

const PathSchema = z.object({ id: z.string().uuid() });

export async function getEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const parsed = PathSchema.safeParse(event.pathParameters ?? {});
  if (!parsed.success) {
    throw ApiError.invalidInput("Path parameter 'id' must be a UUID");
  }
  const { id } = parsed.data;

  // user_id is in the WHERE clause to prevent IDOR — even if a user guesses
  // another user's UUID, they get 404 not 403. Either response leaks
  // existence; 404 leaks less.
  const rows = await query<VaultEntry>(
    `SELECT * FROM vault_entries WHERE id = $1 AND user_id = $2`,
    [id, auth.user_id]
  );
  if (rows.length === 0) {
    throw ApiError.entryNotFound(id);
  }
  return ok(rows[0]);
}
