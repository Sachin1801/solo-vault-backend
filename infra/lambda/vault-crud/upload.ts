import { PutObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import type { AuthContext } from "../shared/auth.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import { created } from "../shared/response.js";

const BUCKET = process.env.VAULT_FILES_BUCKET!;
const s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });

const schema = z.object({
  filename: z.string().min(1).max(500),
  content_type: z.string().min(1).max(200).default("application/octet-stream"),
});

export async function uploadEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext,
): Promise<APIGatewayProxyResult> {
  const entryId = event.pathParameters?.id;
  if (!entryId) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  let body: unknown;
  try {
    body = event.body ? JSON.parse(event.body) : {};
  } catch {
    throw ApiError.invalidInput("Request body is not valid JSON");
  }

  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const path = issue?.path?.join(".") ?? "(root)";
    throw ApiError.invalidInput(`${path}: ${issue?.message ?? "Invalid body"}`);
  }

  const existing = await query<{ id: string }>(
    "SELECT id FROM vault.entries WHERE id = $1 AND user_id = $2",
    [entryId, auth.user_id],
  );
  if (existing.length === 0) {
    throw ApiError.entryNotFound(entryId);
  }

  const { filename, content_type } = parsed.data;
  const s3Key = `${auth.user_id}/${entryId}/${filename}`;
  const expiresIn = 300;

  const uploadUrl = await getSignedUrl(
    s3,
    new PutObjectCommand({ Bucket: BUCKET, Key: s3Key, ContentType: content_type }),
    { expiresIn },
  );

  await query(
    `UPDATE vault.entries
        SET vault_blob_path = $1,
            mime = $2,
            index_status = 'pending',
            cloud_sync_state = 'uploading',
            updated_at = $3
      WHERE id = $4 AND user_id = $5`,
    [s3Key, content_type, Math.floor(Date.now() / 1000), entryId, auth.user_id],
  );

  return created({
    upload_url: uploadUrl,
    presigned_url: uploadUrl,
    s3_key: s3Key,
    content_type,
    expires_in: expiresIn,
  });
}
