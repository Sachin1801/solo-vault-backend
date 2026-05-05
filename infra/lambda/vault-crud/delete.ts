import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import type { AuthContext } from "../shared/auth.js";
import { query } from "../shared/db.js";
import { ApiError } from "../shared/errors.js";
import { ok } from "../shared/response.js";

const PathSchema = z.object({ id: z.string().uuid() });

export async function deleteEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext,
): Promise<APIGatewayProxyResult> {
  const parsed = PathSchema.safeParse(event.pathParameters ?? {});
  if (!parsed.success) {
    throw ApiError.invalidInput("Path parameter 'id' must be a UUID");
  }

  const { id } = parsed.data;
  const rows = await query(
    `DELETE FROM vault_entries WHERE id = $1 AND user_id = $2 RETURNING id, s3_key`,
    [id, auth.user_id],
  );

  if (rows.length === 0) {
    throw ApiError.entryNotFound(id);
  }

  return ok({ deleted: true, id: (rows[0] as { id: string }).id });
}
