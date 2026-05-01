import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import { ok } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import type { VaultEntry } from "../shared/types.js";

const ListQuery = z.object({
  project_id: z.string().uuid().optional(),
  scope: z.enum(["project", "global", "all"]).default("all"),
  tags: z.string().optional(),
  page: z.coerce.number().int().min(1).default(1),
  limit: z.coerce.number().int().min(1).max(100).default(20)
});

interface Row extends VaultEntry {
  total: string; // COUNT(*) OVER () comes back as a numeric string
}

export async function listEntries(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  const parsed = ListQuery.safeParse(event.queryStringParameters ?? {});
  if (!parsed.success) {
    throw ApiError.invalidInput(
      parsed.error.issues[0]?.message ?? "Invalid query"
    );
  }
  const { project_id, scope, tags, page, limit } = parsed.data;

  if (scope === "project" && !project_id) {
    throw ApiError.invalidInput(
      "scope=project requires a project_id query parameter"
    );
  }

  const tagList = tags
    ? tags.split(",").map((t) => t.trim()).filter(Boolean)
    : [];

  // Build the WHERE clause incrementally so each branch contributes its own
  // parameters and we never interpolate user input into the SQL string.
  const conditions: string[] = ["user_id = $1"];
  const params: unknown[] = [auth.user_id];

  if (scope === "project") {
    params.push(project_id);
    conditions.push(`project_id = $${params.length}`);
  } else if (scope === "global") {
    conditions.push("project_id IS NULL");
  }
  // scope === "all" → no additional condition

  if (tagList.length > 0) {
    params.push(tagList);
    // tags @> $N → "row.tags contains all of $N" (AND semantics per issue spec)
    conditions.push(`tags @> $${params.length}`);
  }

  params.push(limit);
  const limitParam = `$${params.length}`;
  params.push((page - 1) * limit);
  const offsetParam = `$${params.length}`;

  const sql = `
    SELECT *, COUNT(*) OVER () AS total
    FROM vault_entries
    WHERE ${conditions.join(" AND ")}
    ORDER BY updated_at DESC
    LIMIT ${limitParam} OFFSET ${offsetParam}
  `;
  const rows = await query<Row>(sql, params);

  const total = rows.length > 0 ? Number(rows[0].total) : 0;
  const entries: VaultEntry[] = rows.map(({ total: _t, ...entry }) => entry);

  return ok({ entries, total, page });
}
