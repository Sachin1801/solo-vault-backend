import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import { ok } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import type { VaultEntry } from "../shared/types.js";

const PathSchema = z.object({ id: z.string().uuid() });

export async function deleteEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const parsed = PathSchema.safeParse(event.pathParameters ?? {});
  if (!parsed.success) {
    throw ApiError.invalidInput("Path parameter 'id' must be a UUID");
  }
  const { id } = parsed.data;

  // Single statement does ownership check + delete + returns the deleted
  // row's s3_key (for the future S3 cleanup step). vault_chunks /
  // vault_chunk_parents cascade automatically via ON DELETE CASCADE in the
  // schema.
  const rows = await query<Pick<VaultEntry, "id" | "s3_key">>(
    `DELETE FROM vault_entries
      WHERE id = $1 AND user_id = $2
     RETURNING id, s3_key`,
    [id, auth.user_id]
  );
  if (rows.length === 0) {
    throw ApiError.entryNotFound(id);
  }

  // TODO(API-3 / INFRA-4): if rows[0].s3_key is set, also delete the S3
  // object. Skipped here because the S3 bucket isn't provisioned yet
  // (INFRA-4 not done) and uploads aren't wired (API-3 not done).
  // Leaving an orphaned S3 object on entry delete is harmless until the
  // bucket exists; it'll just need a one-shot cleanup before demo.

  return ok({ deleted: true, id: rows[0].id });
}
