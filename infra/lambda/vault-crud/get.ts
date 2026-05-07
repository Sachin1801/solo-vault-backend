import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { ok } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import type { VaultEntry } from "../shared/types.js";
import { entryAccessPredicate } from "../shared/vault-authz.js";

export async function getEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const id = event.pathParameters?.id;
  if (!id) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  // IDOR defense: 404 (not 403) when an entry exists but belongs to another
  // user — leaks no existence information.
  const rows = await query<VaultEntry>(
    `SELECT e.* FROM vault.entries e WHERE e.id = $1 AND ${entryAccessPredicate("e", 2, "viewer")}`,
    [id, auth.user_id]
  );

  if (rows.length === 0) {
    throw ApiError.entryNotFound(id);
  }

  return ok(rows[0]);
}
