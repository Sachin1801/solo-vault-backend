import {
  HeadObjectCommand,
  type HeadObjectCommandOutput,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { SFNClient, StartExecutionCommand } from "@aws-sdk/client-sfn";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";
import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import type { AuthContext } from "../shared/auth.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import { created, ok } from "../shared/response.js";
import type { VaultEntry } from "../shared/types.js";
import { entryAccessPredicate } from "../shared/vault-authz.js";

const BUCKET = process.env.VAULT_FILES_BUCKET!;
const s3 = new S3Client({ region: process.env.AWS_REGION ?? "us-east-1" });
const sfn = new SFNClient({ region: process.env.AWS_REGION ?? "us-east-1" });
const INDEX_STATE_MACHINE_ARN = process.env.VAULT_INDEX_STATE_MACHINE_ARN ?? "";

const schema = z.object({
  filename: z.string().min(1).max(500),
  content_type: z.string().min(1).max(200).default("application/octet-stream"),
});

const completeSchema = z
  .object({
    s3_key: z.string().min(1).max(1024).optional(),
  })
  .default({});

export function sanitizeS3Filename(filename: string): string {
  const basename = filename.split(/[\\/]/).pop() ?? "file";
  const normalized = basename
    .normalize("NFKD")
    .replace(/[^A-Za-z0-9._-]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/_+(\.)/g, "$1")
    .replace(/^[._-]+|[._-]+$/g, "");
  return normalized.slice(0, 180) || "file";
}

export function buildVaultObjectKey(userId: string, entryId: string, filename: string): string {
  return `users/${userId}/entries/${entryId}/objects/${sanitizeS3Filename(filename)}`;
}

function parseTags(tags: string): string[] {
  try {
    const parsed = JSON.parse(tags);
    return Array.isArray(parsed) ? parsed.filter((tag) => typeof tag === "string") : [];
  } catch {
    return [];
  }
}

function fileNameFromS3Key(s3Key: string): string {
  return s3Key.split("/").pop() || "file";
}

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
    `SELECT e.id
       FROM vault.entries e
      WHERE e.id = $1 AND ${entryAccessPredicate("e", 2, "editor")}`,
    [entryId, auth.user_id],
  );
  if (existing.length === 0) {
    throw ApiError.entryNotFound(entryId);
  }

  const { filename, content_type } = parsed.data;
  const s3Key = buildVaultObjectKey(auth.user_id, entryId, filename);
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
            index_error = NULL,
            updated_at = $3
      WHERE id = $4`,
    [s3Key, content_type, Math.floor(Date.now() / 1000), entryId],
  );

  return created({
    upload_url: uploadUrl,
    presigned_url: uploadUrl,
    s3_key: s3Key,
    content_type,
    expires_in: expiresIn,
  });
}

export async function completeUploadEntry(
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

  const parsed = completeSchema.safeParse(body);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const path = issue?.path?.join(".") ?? "(root)";
    throw ApiError.invalidInput(`${path}: ${issue?.message ?? "Invalid body"}`);
  }

  const rows = await query<VaultEntry>(
    `SELECT e.*
       FROM vault.entries e
      WHERE e.id = $1 AND ${entryAccessPredicate("e", 2, "editor")}`,
    [entryId, auth.user_id],
  );
  const entry = rows[0];
  if (!entry) {
    throw ApiError.entryNotFound(entryId);
  }
  if (!entry.vault_blob_path) {
    throw ApiError.invalidInput("Entry does not have an uploaded object key");
  }
  if (parsed.data.s3_key && parsed.data.s3_key !== entry.vault_blob_path) {
    throw ApiError.invalidInput("Uploaded object key does not match the entry");
  }
  if (!INDEX_STATE_MACHINE_ARN) {
    throw ApiError.internal("VAULT_INDEX_STATE_MACHINE_ARN environment variable is not set");
  }

  let head: HeadObjectCommandOutput;
  try {
    head = await s3.send(
      new HeadObjectCommand({ Bucket: BUCKET, Key: entry.vault_blob_path }),
    );
  } catch (err) {
    const status = (err as { $metadata?: { httpStatusCode?: number } }).$metadata?.httpStatusCode;
    const name = (err as { name?: string }).name;
    if (status === 404 || name === "NotFound" || name === "NoSuchKey") {
      throw ApiError.invalidInput("Uploaded S3 object was not found");
    }
    throw err;
  }
  const contentType = head.ContentType || entry.mime || "application/octet-stream";
  const sizeBytes = head.ContentLength ?? entry.size_bytes ?? 0;
  const now = Math.floor(Date.now() / 1000);

  const updatedRows = await query<VaultEntry>(
    `UPDATE vault.entries
        SET cloud_sync_state = 'indexing_remote',
            index_status = 'pending',
            size_bytes = $1,
            mime = $2,
            uploaded_at = $3,
            updated_at = $3,
            index_error = NULL
      WHERE id = $4
      RETURNING *`,
    [sizeBytes, contentType, now, entryId],
  );
  const updated = updatedRows[0] ?? entry;

  try {
    await sfn.send(
      new StartExecutionCommand({
        stateMachineArn: INDEX_STATE_MACHINE_ARN,
        input: JSON.stringify({
          entry_id: updated.id,
          user_id: updated.user_id,
          bucket: BUCKET,
          s3_key: updated.vault_blob_path,
          file_name: fileNameFromS3Key(updated.vault_blob_path ?? ""),
          mime: updated.mime ?? contentType,
          kind: updated.kind,
          subkind: updated.subkind ?? "",
          size_bytes: updated.size_bytes ?? sizeBytes,
          title: updated.title,
          tags: parseTags(updated.tags),
          classifier_confidence: updated.classifier_confidence,
          pinned: Boolean(updated.pinned),
          memory_type: updated.memory_type,
          project_id: updated.scope_project_id,
        }),
      }),
    );
  } catch (err) {
    await query(
      `UPDATE vault.entries
          SET cloud_sync_state = 'failed',
              index_status = 'failed',
              index_error = $1,
              updated_at = $2
        WHERE id = $3`,
      [err instanceof Error ? err.message : String(err), now, entryId],
    );
    throw err;
  }

  return ok({
    entry_id: updated.id,
    s3_key: updated.vault_blob_path,
    index_status: "pending",
    cloud_sync_state: "indexing_remote",
    indexing_started: true,
  });
}
