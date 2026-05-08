import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import type { AuthContext } from "../shared/auth.js";
import { query } from "../shared/db.js";
import { ApiError } from "../shared/errors.js";
import { ok } from "../shared/response.js";
import type { VaultEntry } from "../shared/types.js";
import { entryAccessPredicate } from "../shared/vault-authz.js";
import { sanitizeS3Filename } from "./upload.js";

const BUCKET = process.env.VAULT_FILES_BUCKET!;
const s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });

function filenameForEntry(entry: VaultEntry): string {
  const fromKey = entry.vault_blob_path?.split("/").pop();
  return sanitizeS3Filename(fromKey || entry.title || "file");
}

export async function downloadEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext,
): Promise<APIGatewayProxyResult> {
  const id = event.pathParameters?.id;
  if (!id) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  const rows = await query<VaultEntry>(
    `SELECT e.*
       FROM vault.entries e
      WHERE e.id = $1 AND ${entryAccessPredicate("e", 2, "viewer")}`,
    [id, auth.user_id],
  );
  const entry = rows[0];
  if (!entry) {
    throw ApiError.entryNotFound(id);
  }
  if (!entry.vault_blob_path) {
    throw ApiError.invalidInput("Entry does not have an uploaded object");
  }

  const expiresIn = 300;
  const filename = filenameForEntry(entry);
  const downloadUrl = await getSignedUrl(
    s3,
    new GetObjectCommand({
      Bucket: BUCKET,
      Key: entry.vault_blob_path,
      ResponseContentDisposition: `attachment; filename="${filename}"`,
      ResponseContentType: entry.mime ?? undefined,
    }),
    { expiresIn },
  );

  return ok({
    download_url: downloadUrl,
    presigned_url: downloadUrl,
    s3_key: entry.vault_blob_path,
    content_type: entry.mime,
    expires_in: expiresIn,
  });
}
