import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { z } from "zod";
import type { AuthContext } from "../shared/auth.js";
import { query } from "../shared/db.js";
import { ApiError } from "../shared/errors.js";
import { ok } from "../shared/response.js";
import { ENTRY_KINDS, SCOPE_TYPES } from "../shared/types.js";
import { entryAccessPredicate } from "../shared/vault-authz.js";

const SearchRequest = z
  .object({
    query: z.string().min(1).max(1000),
    limit: z.number().int().min(1).max(50).default(20),
    scope_type: z.enum(SCOPE_TYPES).optional(),
    scope_project_id: z.string().optional(),
    kind: z.enum(ENTRY_KINDS).optional(),
  })
  .refine((q) => (q.scope_type === "project" ? !!q.scope_project_id : true), {
    message: "scope_project_id required when scope_type='project'",
    path: ["scope_project_id"],
  });

interface SearchRow {
  entry_id: string;
  chunk_id: string;
  chunk_index: number;
  snippet: string;
  score: number;
  entry: Record<string, unknown>;
}

export async function searchEntries(
  event: APIGatewayProxyEvent,
  auth: AuthContext,
): Promise<APIGatewayProxyResult> {
  let body: unknown;
  try {
    body = event.body ? JSON.parse(event.body) : {};
  } catch {
    throw ApiError.invalidInput("Request body is not valid JSON");
  }

  const parsed = SearchRequest.safeParse(body);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const path = issue?.path?.join(".") ?? "(root)";
    throw ApiError.invalidInput(`${path}: ${issue?.message ?? "Invalid body"}`);
  }

  const input = parsed.data;
  const conditions: string[] = [
    entryAccessPredicate("e", 1, "viewer"),
    "to_tsvector('english', f.content) @@ websearch_to_tsquery('english', $2)",
  ];
  const params: unknown[] = [auth.user_id, input.query];
  let p = 3;

  if (input.scope_type) {
    conditions.push(`e.scope_type = $${p++}`);
    params.push(input.scope_type);
    if (input.scope_type === "project") {
      conditions.push(`e.scope_project_id = $${p++}`);
      params.push(input.scope_project_id);
    } else {
      conditions.push("e.scope_project_id IS NULL");
    }
  }
  if (input.kind) {
    conditions.push(`e.kind = $${p++}`);
    params.push(input.kind);
  }
  params.push(input.limit);

  const rows = await query<SearchRow>(
    `SELECT
        e.id AS entry_id,
        f.chunk_id,
        c.chunk_index,
        ts_headline(
          'english',
          f.content,
          websearch_to_tsquery('english', $2),
          'MaxWords=35, MinWords=8, ShortWord=3'
        ) AS snippet,
        ts_rank_cd(
          to_tsvector('english', f.content),
          websearch_to_tsquery('english', $2)
        ) AS score,
        to_jsonb(e.*) AS entry
       FROM vault.chunks_fts f
       JOIN vault.entries e ON e.id = f.entry_id
       LEFT JOIN vault.chunks c ON c.id = f.chunk_id
      WHERE ${conditions.join(" AND ")}
      ORDER BY score DESC, e.updated_at DESC
      LIMIT $${p}`,
    params,
  );

  return ok({
    query: input.query,
    results: rows.map((row) => ({
      entry_id: row.entry_id,
      chunk_id: row.chunk_id,
      chunk_index: Number(row.chunk_index),
      snippet: row.snippet,
      score: Number(row.score),
      entry: row.entry,
    })),
  });
}
