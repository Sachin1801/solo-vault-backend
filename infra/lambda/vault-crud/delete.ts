import { DeleteObjectCommand, S3Client } from "@aws-sdk/client-s3";
import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { ok } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query, getPool } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import { entryAccessPredicate } from "../shared/vault-authz.js";

const BUCKET = process.env.VAULT_FILES_BUCKET!;
const s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });

function logVaultDelete(event: string, fields: Record<string, unknown>): void {
  console.info(JSON.stringify({ event, service: "vault-crud", ...fields }));
}

export async function deleteEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const id = event.pathParameters?.id;
  if (!id) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  logVaultDelete("vault.delete.requested", {
    entry_id: id,
    user_id: auth.user_id,
    bucket: BUCKET,
  });

  const entries = await query<{ id: string; vault_blob_path: string | null }>(
    `SELECT e.id, e.vault_blob_path
       FROM vault.entries e
      WHERE e.id = $1 AND ${entryAccessPredicate("e", 2, "owner")}`,
    [id, auth.user_id],
  );
  const entry = entries[0];
  if (!entry) {
    logVaultDelete("vault.delete.not_found_or_unauthorized", {
      entry_id: id,
      user_id: auth.user_id,
    });
    throw ApiError.entryNotFound(id);
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
      `DELETE FROM vault.chunks_fts WHERE entry_id = $1`,
      [id]
    );
    const result = await client.query<{ id: string }>(
      `DELETE FROM vault.entries
         WHERE id = $1
         RETURNING id`,
      [id]
    );
    await client.query("COMMIT");

    if (result.rowCount === 0) {
      throw ApiError.entryNotFound(id);
    }
    logVaultDelete("vault.delete.db_deleted", {
      entry_id: id,
      user_id: auth.user_id,
      had_s3_object: Boolean(entry.vault_blob_path),
    });
    if (entry.vault_blob_path) {
      await s3.send(new DeleteObjectCommand({ Bucket: BUCKET, Key: entry.vault_blob_path }));
      logVaultDelete("vault.delete.s3_deleted", {
        entry_id: id,
        user_id: auth.user_id,
        bucket: BUCKET,
        s3_key: entry.vault_blob_path,
      });
    }

    logVaultDelete("vault.delete.completed", {
      entry_id: id,
      user_id: auth.user_id,
    });
    return ok({ deleted: true, id: result.rows[0].id });
  } catch (err) {
    logVaultDelete("vault.delete.failed", {
      entry_id: id,
      user_id: auth.user_id,
      error: err instanceof Error ? err.message : String(err),
    });
    await client.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}
