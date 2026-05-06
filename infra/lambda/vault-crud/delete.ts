import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { ok } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query, getPool } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";

export async function deleteEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const id = event.pathParameters?.id;
  if (!id) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  // Two-statement transaction. vault.chunks_fts has no FK to vault.entries
  // (matches local SQLite where chunks_fts is a virtual FTS5 table) — local
  // store deletes from chunks_fts manually before deleting chunks. Mirror
  // that here so we don't leak FTS rows after the entry is gone.
  const pool = await getPool();
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(
      `DELETE FROM vault.chunks_fts WHERE entry_id = $1 AND user_id = $2`,
      [id, auth.user_id]
    );
    const result = await client.query<{ id: string }>(
      `DELETE FROM vault.entries
         WHERE id = $1 AND user_id = $2
         RETURNING id`,
      [id, auth.user_id]
    );
    await client.query("COMMIT");

    if (result.rowCount === 0) {
      throw ApiError.entryNotFound(id);
    }
    return ok({ deleted: true, id: result.rows[0].id });
  } catch (err) {
    await client.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}
