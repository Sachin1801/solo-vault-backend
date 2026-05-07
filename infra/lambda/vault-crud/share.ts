import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import type { PoolClient } from "pg";
import type { AuthContext } from "../shared/auth.js";
import { getPool, query } from "../shared/db.js";
import { ApiError } from "../shared/errors.js";
import { ok } from "../shared/response.js";
import { entryAccessPredicate } from "../shared/vault-authz.js";

const SHARE_SUBJECT_TYPES = ["user", "organization", "project"] as const;
const SHARE_ROLES = ["owner", "editor", "viewer"] as const;

const ShareGrantSchema = z.object({
  subject_type: z.enum(SHARE_SUBJECT_TYPES),
  subject_id: z.string().trim().min(1).max(300),
  role: z.enum(SHARE_ROLES),
});

const ReplaceSharesSchema = z.object({
  shares: z.array(ShareGrantSchema).max(100),
});

const DeleteShareSchema = z.object({
  subject_type: z.enum(SHARE_SUBJECT_TYPES),
  subject_id: z.string().trim().min(1).max(300),
});

type ShareSubjectType = (typeof SHARE_SUBJECT_TYPES)[number];
type ShareRole = (typeof SHARE_ROLES)[number];

interface OwnerEntryRow {
  id: string;
  user_id: string;
  owner_user_id: string | null;
}

interface EntryShareRow {
  entry_id: string;
  subject_type: ShareSubjectType;
  subject_id: string;
  role: ShareRole;
  created_at: number;
  updated_at: number;
}

function parseJsonBody(event: APIGatewayProxyEvent): unknown {
  try {
    return event.body ? JSON.parse(event.body) : {};
  } catch {
    throw ApiError.invalidInput("Request body is not valid JSON");
  }
}

function zodMessage(error: z.ZodError): string {
  const issue = error.issues[0];
  const path = issue?.path?.join(".") ?? "(root)";
  return `${path}: ${issue?.message ?? "Invalid body"}`;
}

function shareKey(share: { subject_type: ShareSubjectType; subject_id: string }): string {
  return `${share.subject_type}:${share.subject_id}`;
}

function effectiveOwnerUserId(entry: OwnerEntryRow): string {
  return entry.owner_user_id ?? entry.user_id;
}

async function requireOwnerEntry(
  entryId: string,
  auth: AuthContext,
): Promise<OwnerEntryRow> {
  const rows = await query<OwnerEntryRow>(
    `SELECT e.id, e.user_id, e.owner_user_id
       FROM vault.entries e
      WHERE e.id = $1 AND ${entryAccessPredicate("e", 2, "owner")}`,
    [entryId, auth.user_id],
  );
  const entry = rows[0];
  if (!entry) {
    throw ApiError.entryNotFound(entryId);
  }
  return entry;
}

async function validateSubjectExists(
  client: PoolClient,
  subjectType: ShareSubjectType,
  subjectId: string,
): Promise<void> {
  if (subjectType === "user") {
    // Cognito is the source of truth for users; this service has no users table.
    return;
  }

  const table =
    subjectType === "organization" ? "vault.organizations" : "vault.projects";
  const result = await client.query(`SELECT 1 FROM ${table} WHERE id = $1`, [subjectId]);
  if (result.rowCount === 0) {
    throw ApiError.invalidInput(`${subjectType} '${subjectId}' does not exist`);
  }
}

function validateGrantTarget(
  grant: z.infer<typeof ShareGrantSchema>,
  entry: OwnerEntryRow,
): void {
  if (
    grant.subject_type === "user" &&
    grant.subject_id === effectiveOwnerUserId(entry)
  ) {
    throw ApiError.invalidInput("Entry owner already has owner access");
  }
}

async function loadShares(entryId: string): Promise<EntryShareRow[]> {
  return await query<EntryShareRow>(
    `SELECT entry_id, subject_type, subject_id, role, created_at, updated_at
       FROM vault.entry_access
      WHERE entry_id = $1
      ORDER BY subject_type ASC, subject_id ASC`,
    [entryId],
  );
}

export async function listEntryShares(
  event: APIGatewayProxyEvent,
  auth: AuthContext,
): Promise<APIGatewayProxyResult> {
  const entryId = event.pathParameters?.id;
  if (!entryId) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  const entry = await requireOwnerEntry(entryId, auth);
  const shares = await loadShares(entryId);

  return ok({
    entry_id: entry.id,
    owner_user_id: effectiveOwnerUserId(entry),
    shares,
  });
}

export async function upsertEntryShare(
  event: APIGatewayProxyEvent,
  auth: AuthContext,
): Promise<APIGatewayProxyResult> {
  const entryId = event.pathParameters?.id;
  if (!entryId) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  const parsed = ShareGrantSchema.safeParse(parseJsonBody(event));
  if (!parsed.success) {
    throw ApiError.invalidInput(zodMessage(parsed.error));
  }
  const grant = parsed.data;
  const entry = await requireOwnerEntry(entryId, auth);
  validateGrantTarget(grant, entry);

  const now = Math.floor(Date.now() / 1000);
  const pool = await getPool();
  const client = await pool.connect();
  try {
    await validateSubjectExists(client, grant.subject_type, grant.subject_id);
    const result = await client.query<EntryShareRow>(
      `INSERT INTO vault.entry_access
        (entry_id, subject_type, subject_id, role, created_at, updated_at)
       VALUES ($1, $2, $3, $4, $5, $5)
       ON CONFLICT (entry_id, subject_type, subject_id)
       DO UPDATE SET role = EXCLUDED.role, updated_at = EXCLUDED.updated_at
       RETURNING entry_id, subject_type, subject_id, role, created_at, updated_at`,
      [entryId, grant.subject_type, grant.subject_id, grant.role, now],
    );

    return ok({
      entry_id: entry.id,
      owner_user_id: effectiveOwnerUserId(entry),
      share: result.rows[0],
    });
  } finally {
    client.release();
  }
}

export async function replaceEntryShares(
  event: APIGatewayProxyEvent,
  auth: AuthContext,
): Promise<APIGatewayProxyResult> {
  const entryId = event.pathParameters?.id;
  if (!entryId) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  const parsed = ReplaceSharesSchema.safeParse(parseJsonBody(event));
  if (!parsed.success) {
    throw ApiError.invalidInput(zodMessage(parsed.error));
  }

  const entry = await requireOwnerEntry(entryId, auth);
  const seen = new Set<string>();
  for (const grant of parsed.data.shares) {
    validateGrantTarget(grant, entry);
    const key = shareKey(grant);
    if (seen.has(key)) {
      throw ApiError.invalidInput(`Duplicate share target: ${key}`);
    }
    seen.add(key);
  }

  const now = Math.floor(Date.now() / 1000);
  const pool = await getPool();
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    for (const grant of parsed.data.shares) {
      await validateSubjectExists(client, grant.subject_type, grant.subject_id);
    }

    await client.query("DELETE FROM vault.entry_access WHERE entry_id = $1", [entryId]);
    for (const grant of parsed.data.shares) {
      await client.query(
        `INSERT INTO vault.entry_access
          (entry_id, subject_type, subject_id, role, created_at, updated_at)
         VALUES ($1, $2, $3, $4, $5, $5)`,
        [entryId, grant.subject_type, grant.subject_id, grant.role, now],
      );
    }
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK");
    throw err;
  } finally {
    client.release();
  }

  return ok({
    entry_id: entry.id,
    owner_user_id: effectiveOwnerUserId(entry),
    shares: await loadShares(entryId),
  });
}

export async function deleteEntryShare(
  event: APIGatewayProxyEvent,
  auth: AuthContext,
): Promise<APIGatewayProxyResult> {
  const entryId = event.pathParameters?.id;
  if (!entryId) {
    throw ApiError.invalidInput("Path parameter {id} is required");
  }

  const parsed = DeleteShareSchema.safeParse(parseJsonBody(event));
  if (!parsed.success) {
    throw ApiError.invalidInput(zodMessage(parsed.error));
  }
  const share = parsed.data;
  const entry = await requireOwnerEntry(entryId, auth);

  const rows = await query<{ deleted: string }>(
    `DELETE FROM vault.entry_access
      WHERE entry_id = $1 AND subject_type = $2 AND subject_id = $3
      RETURNING '1' AS deleted`,
    [entryId, share.subject_type, share.subject_id],
  );

  return ok({
    entry_id: entry.id,
    owner_user_id: effectiveOwnerUserId(entry),
    subject_type: share.subject_type,
    subject_id: share.subject_id,
    deleted: rows.length > 0,
  });
}
