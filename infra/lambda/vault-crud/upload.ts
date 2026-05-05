import { PutObjectCommand, S3Client } from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import type { AuthContext } from "../shared/auth.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import { created, handleError } from "../shared/response.js";

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
  try {
    const entry_id = event.pathParameters?.id;
    if (!entry_id) throw ApiError.invalidInput("Missing entry id");

    const existing = await query<{ id: string }>(
      "SELECT id FROM vault_entries WHERE id = $1 AND user_id = $2",
      [entry_id, auth.user_id],
    );
    if (existing.rows.length === 0) throw ApiError.entryNotFound();

    const parsed = schema.safeParse(JSON.parse(event.body ?? "{}"));
    if (!parsed.success) throw ApiError.invalidInput(parsed.error.message);

    const { filename, content_type } = parsed.data;
    const s3_key = `${auth.user_id}/${entry_id}/${filename}`;

    const presigned_url = await getSignedUrl(
      s3,
      new PutObjectCommand({ Bucket: BUCKET, Key: s3_key, ContentType: content_type }),
      { expiresIn: 300 },
    );

    await query(
      "UPDATE vault_entries SET s3_key = $1, index_status = 'pending', updated_at = NOW() WHERE id = $2 AND user_id = $3",
      [s3_key, entry_id, auth.user_id],
    );

    return created({ presigned_url, s3_key, content_type, expires_in: 300 });
  } catch (err) {
    return handleError(err);
  }
}
