import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import { ok } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import {
  ENTRY_KINDS,
  MEMORY_TYPES,
  SCOPE_TYPES,
  type VaultEntry
} from "../shared/types.js";

// Coerce query-string values (always strings) into the proper types.
const ListQuery = z
  .object({
    scope_type: z.enum(SCOPE_TYPES).optional(),
    scope_project_id: z.string().optional(),
    kind: z.enum(ENTRY_KINDS).optional(),
    memory_type: z.enum(MEMORY_TYPES).optional(),
    pinned: z.union([z.literal("0"), z.literal("1")]).optional(),
    tag: z.string().optional(),
    page: z.coerce.number().int().min(1).default(1),
    limit: z.coerce.number().int().min(1).max(100).default(20)
  })
  .refine(
    (q) => (q.scope_type === "project" ? !!q.scope_project_id : true),
    { message: "scope_project_id required when scope_type='project'", path: ["scope_project_id"] }
  );

export async function listEntries(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const parsed = ListQuery.safeParse(event.queryStringParameters ?? {});
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const path = issue?.path?.join(".") ?? "(query)";
    throw ApiError.invalidInput(`${path}: ${issue?.message ?? "Invalid query"}`);
  }
  const q = parsed.data;

  // Build WHERE incrementally so missing filters don't appear in the SQL.
  const conditions: string[] = ["user_id = $1"];
  const params: unknown[] = [auth.user_id];
  let p = 2;

  if (q.scope_type) {
    conditions.push(`scope_type = $${p++}`);
    params.push(q.scope_type);
    if (q.scope_type === "project") {
      conditions.push(`scope_project_id = $${p++}`);
      params.push(q.scope_project_id);
    } else {
      conditions.push(`scope_project_id IS NULL`);
    }
  }
  if (q.kind) {
    conditions.push(`kind = $${p++}`);
    params.push(q.kind);
  }
  if (q.memory_type) {
    conditions.push(`memory_type = $${p++}`);
    params.push(q.memory_type);
  }
  if (q.pinned !== undefined) {
    conditions.push(`pinned = $${p++}`);
    params.push(Number(q.pinned));
  }
  if (q.tag) {
    // tags column is TEXT (JSON-string mirror of local). Cast to jsonb on
    // read for containment query — index-less but acceptable while
    // entry counts are low. Add a generated jsonb column + GIN later if hot.
    conditions.push(`tags::jsonb @> $${p++}::jsonb`);
    params.push(JSON.stringify([q.tag]));
  }

  const offset = (q.page - 1) * q.limit;
  params.push(q.limit, offset);

  // Single query with COUNT(*) OVER () gives total alongside the page.
  const rows = await query<VaultEntry & { total: string }>(
    `SELECT *, COUNT(*) OVER () AS total
       FROM vault.entries
      WHERE ${conditions.join(" AND ")}
      ORDER BY updated_at DESC
      LIMIT $${p++} OFFSET $${p}`,
    params
  );

  const total = rows.length > 0 ? Number(rows[0].total) : 0;
  // Strip the COUNT col from each row before responding.
  const entries = rows.map(({ total: _t, ...rest }) => rest);

  return ok({ entries, total, page: q.page, limit: q.limit });
}
