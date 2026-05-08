import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { InvokeCommand, LambdaClient } from "@aws-sdk/client-lambda";
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
    mode: z.enum(["fts", "semantic", "hybrid"]).default("fts"),
    scope_type: z.enum(SCOPE_TYPES).optional(),
    scope_project_id: z.string().optional(),
    kind: z.enum(ENTRY_KINDS).optional(),
    // Test/dev escape hatch. Production clients should let the backend invoke
    // the query embedder so the embedding contract stays centralized.
    query_embedding: z.array(z.number()).length(384).optional(),
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
  source?: "cloud";
  mode?: SearchMode;
  embedding_model?: string | null;
  chunker_version?: string | null;
}

type SearchInput = z.infer<typeof SearchRequest>;
type SearchMode = SearchInput["mode"];

const QUERY_EMBED_FUNCTION_NAME =
  process.env.VAULT_QUERY_EMBED_FUNCTION_NAME ??
  process.env.QUERY_EMBED_FUNCTION_NAME ??
  "";
const QUERY_EMBEDDING_MODEL = "BAAI/bge-m3";
const QUERY_EMBEDDING_DIM = 384;
const RRF_K = 60;

let lambdaClient: LambdaClient | null = null;

function getLambdaClient(): LambdaClient {
  lambdaClient ??= new LambdaClient({});
  return lambdaClient;
}

function buildEntryFilters(input: SearchInput, auth: AuthContext): {
  conditions: string[];
  params: unknown[];
  nextParam: number;
} {
  const conditions: string[] = [entryAccessPredicate("e", 1, "viewer")];
  const params: unknown[] = [auth.user_id];
  let p = 2;

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

  return { conditions, params, nextParam: p };
}

function vectorLiteral(values: number[]): string {
  if (values.length !== QUERY_EMBEDDING_DIM) {
    throw ApiError.invalidInput(`query_embedding must have ${QUERY_EMBEDDING_DIM} dimensions`);
  }

  const cleaned = values.map((value) => {
    if (!Number.isFinite(value)) {
      throw ApiError.invalidInput("query_embedding contains a non-finite number");
    }
    return Number(value);
  });

  return `[${cleaned.join(",")}]`;
}

async function embedQuery(input: SearchInput): Promise<number[]> {
  if (input.query_embedding) return input.query_embedding;
  if (!QUERY_EMBED_FUNCTION_NAME) {
    throw ApiError.internal("Query embedding function is not configured");
  }

  const response = await getLambdaClient().send(
    new InvokeCommand({
      FunctionName: QUERY_EMBED_FUNCTION_NAME,
      InvocationType: "RequestResponse",
      Payload: Buffer.from(JSON.stringify({ query: input.query })),
    }),
  );

  const raw = Buffer.from(response.Payload ?? new Uint8Array()).toString("utf-8");
  if (response.FunctionError) {
    throw ApiError.internal(`Query embedding failed: ${raw || response.FunctionError}`);
  }

  let payload: unknown;
  try {
    payload = raw ? JSON.parse(raw) : {};
  } catch {
    throw ApiError.internal("Query embedding response was not valid JSON");
  }

  const parsed = z
    .object({
      embedding: z.array(z.number()).length(QUERY_EMBEDDING_DIM),
      embedding_model: z.literal(QUERY_EMBEDDING_MODEL),
      embedding_dim: z.literal(QUERY_EMBEDDING_DIM),
    })
    .safeParse(payload);
  if (!parsed.success) {
    throw ApiError.internal("Query embedding response did not match the contract");
  }

  return parsed.data.embedding;
}

async function ftsSearch(input: SearchInput, auth: AuthContext): Promise<SearchRow[]> {
  const { conditions, params, nextParam } = buildEntryFilters(input, auth);
  let p = nextParam;
  const queryParam = p++;
  conditions.push(
    `to_tsvector('english', f.content) @@ websearch_to_tsquery('english', $${queryParam})`,
  );
  params.push(input.query);
  const limitParam = p++;
  params.push(input.limit);

  const rows = await query<SearchRow>(
    `SELECT
        e.id AS entry_id,
        f.chunk_id,
        COALESCE(c.chunk_index, 0) AS chunk_index,
        ts_headline(
          'english',
          f.content,
          websearch_to_tsquery('english', $${queryParam}),
          'MaxWords=35, MinWords=8, ShortWord=3'
        ) AS snippet,
        ts_rank_cd(
          to_tsvector('english', f.content),
          websearch_to_tsquery('english', $${queryParam})
        ) AS score,
        to_jsonb(e.*) AS entry,
        e.embedding_model,
        e.chunker_version
       FROM vault.chunks_fts f
       JOIN vault.entries e ON e.id = f.entry_id
       LEFT JOIN vault.chunks c ON c.id = f.chunk_id
      WHERE ${conditions.join(" AND ")}
      ORDER BY score DESC, e.updated_at DESC
      LIMIT $${limitParam}`,
    params,
  );

  return rows.map((row) => ({
    ...row,
    score: Number(row.score),
    source: "cloud",
    mode: "fts",
  }));
}

async function semanticSearch(input: SearchInput, auth: AuthContext): Promise<SearchRow[]> {
  const embedding = await embedQuery(input);
  const vector = vectorLiteral(embedding);
  const { conditions, params, nextParam } = buildEntryFilters(input, auth);
  let p = nextParam;
  const vectorParam = p++;
  params.push(vector);
  const limitParam = p++;
  params.push(input.limit);

  conditions.push("e.index_status = 'indexed'");
  conditions.push("e.cloud_sync_state = 'synced'");
  conditions.push("c.embedding IS NOT NULL");
  conditions.push("e.embedding_model = $".concat(String(p++)));
  params.push(QUERY_EMBEDDING_MODEL);
  conditions.push("e.chunker_version = '1'");

  const rows = await query<SearchRow>(
    `SELECT
        e.id AS entry_id,
        c.id AS chunk_id,
        c.chunk_index,
        c.content AS snippet,
        1 - (c.embedding <=> $${vectorParam}::vector) AS score,
        to_jsonb(e.*) AS entry,
        e.embedding_model,
        e.chunker_version
       FROM vault.chunks c
       JOIN vault.entries e ON e.id = c.entry_id
      WHERE ${conditions.join(" AND ")}
      ORDER BY c.embedding <=> $${vectorParam}::vector, e.updated_at DESC
      LIMIT $${limitParam}`,
    params,
  );

  return rows.map((row) => ({
    ...row,
    score: Number(row.score),
    source: "cloud",
    mode: "semantic",
  }));
}

function fuseResults(lists: SearchRow[][]): SearchRow[] {
  const fused = new Map<string, SearchRow & { fused_score: number }>();

  for (const rows of lists) {
    rows.forEach((row, index) => {
      const key = `${row.entry_id}:${row.chunk_index}`;
      const contribution = 1 / (RRF_K + index + 1);
      const existing = fused.get(key);
      if (existing) {
        existing.fused_score += contribution;
        if (row.score > existing.score) {
          existing.snippet = row.snippet;
          existing.score = row.score;
        }
        existing.mode = "hybrid";
      } else {
        fused.set(key, {
          ...row,
          fused_score: contribution,
          mode: "hybrid",
        });
      }
    });
  }

  const sorted = [...fused.values()].sort((a, b) => {
    if (b.fused_score !== a.fused_score) return b.fused_score - a.fused_score;
    if (b.score !== a.score) return b.score - a.score;
    return `${a.entry_id}:${a.chunk_index}`.localeCompare(`${b.entry_id}:${b.chunk_index}`);
  });
  const topScore = sorted[0]?.fused_score ?? 1;
  return sorted.map(({ fused_score, ...row }) => ({
    ...row,
    score: topScore > 0 ? fused_score / topScore : 0,
  }));
}

function serializeRow(row: SearchRow) {
  return {
    entry_id: row.entry_id,
    chunk_id: row.chunk_id,
    chunk_index: Number(row.chunk_index),
    snippet: row.snippet,
    score: Number(row.score),
    source: "cloud",
    mode: row.mode,
    embedding_model: row.embedding_model ?? undefined,
    chunker_version: row.chunker_version ?? undefined,
    entry: row.entry,
  };
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
  let rows: SearchRow[];
  let cloud_error: string | undefined;

  if (input.mode === "semantic") {
    rows = await semanticSearch(input, auth);
  } else if (input.mode === "hybrid") {
    const ftsRows = await ftsSearch(input, auth);
    try {
      const semanticRows = await semanticSearch(input, auth);
      rows = fuseResults([ftsRows, semanticRows]).slice(0, input.limit);
    } catch (err) {
      cloud_error = err instanceof Error ? err.message : String(err);
      console.warn("vault hybrid semantic fallback:", cloud_error);
      rows = ftsRows;
    }
  } else {
    rows = await ftsSearch(input, auth);
  }

  return ok({
    query: input.query,
    source: "cloud",
    mode: input.mode,
    cloud_error,
    results: rows.map(serializeRow),
  });
}
