import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { InvokeCommand, LambdaClient } from "@aws-sdk/client-lambda";
import { z } from "zod";
import { created } from "../shared/response.js";
import { ApiError } from "../shared/errors.js";
import { query } from "../shared/db.js";
import type { AuthContext } from "../shared/auth.js";
import {
  CLOUD_SYNC_STATES,
  ENTRY_KINDS,
  INDEX_STATUSES,
  MEMORY_TYPES,
  SCOPE_TYPES,
  type VaultEntry
} from "../shared/types.js";

const QUERY_EMBED_FUNCTION_NAME =
  process.env.VAULT_QUERY_EMBED_FUNCTION_NAME ??
  process.env.QUERY_EMBED_FUNCTION_NAME ??
  "";
const EMBEDDING_MODEL = "BAAI/bge-m3";
const EMBEDDING_DIM = 384;
const CHUNKER_VERSION = "1";
const CHUNK_WORDS = 500;
const CHUNK_OVERLAP_WORDS = 50;

let lambdaClient: LambdaClient | null = null;

function getLambdaClient(): LambdaClient {
  lambdaClient ??= new LambdaClient({});
  return lambdaClient;
}

// tags is stored as a JSON-string in vault.entries (local-parity). Validate
// that it parses to a string[] but keep storing as text — pgsql casts on read
// where filtering is needed (`tags::jsonb @> $N`).
const tagsJsonString = z.string().refine((value) => {
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) && parsed.every((t) => typeof t === "string");
  } catch {
    return false;
  }
}, "tags must be a JSON-encoded array of strings");

const CreateRequest = z
  .object({
    id: z.string().min(1),
    kind: z.enum(ENTRY_KINDS),
    subkind: z.string().nullable().optional(),
    title: z.string().min(1).max(500),
    content: z.string().nullable().optional(),
    source_path: z.string().nullable().optional(),
    vault_blob_path: z.string().nullable().optional(),
    scope_type: z.enum(SCOPE_TYPES),
    scope_project_id: z.string().nullable().optional(),
    memory_type: z.enum(MEMORY_TYPES),
    pinned: z.union([z.literal(0), z.literal(1)]).default(0),
    tags: tagsJsonString.default("[]"),
    mime: z.string().nullable().optional(),
    size_bytes: z.number().int().nonnegative().nullable().optional(),
    index_status: z.enum(INDEX_STATUSES),
    cloud_sync_state: z.enum(CLOUD_SYNC_STATES),
    classifier_confidence: z.number().min(0).max(1),
    hit_count: z.number().int().nonnegative().default(0),
    last_retrieved_at: z.number().int().nullable().optional(),
    created_at: z.number().int(),
    updated_at: z.number().int()
  })
  .refine(
    (data) =>
      data.scope_type === "project" ? !!data.scope_project_id : true,
    { message: "scope_project_id required when scope_type='project'", path: ["scope_project_id"] }
  )
  .refine(
    (data) =>
      data.scope_type === "global" ? !data.scope_project_id : true,
    { message: "scope_project_id must be omitted when scope_type='global'", path: ["scope_project_id"] }
  );

function vectorLiteral(values: number[]): string {
  if (values.length !== EMBEDDING_DIM) {
    throw new Error(`embedding must have ${EMBEDDING_DIM} dimensions`);
  }
  return `[${values.map((value) => Number(value)).join(",")}]`;
}

function chunkText(text: string): Array<{ content: string; token_count: number }> {
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return [];
  const step = Math.max(1, CHUNK_WORDS - CHUNK_OVERLAP_WORDS);
  const chunks: Array<{ content: string; token_count: number }> = [];
  for (let i = 0; i < words.length;) {
    const end = Math.min(words.length, i + CHUNK_WORDS);
    const content = words.slice(i, end).join(" ");
    chunks.push({
      content,
      token_count: Math.round(content.split(/\s+/).filter(Boolean).length * 1.3)
    });
    if (end === words.length) break;
    i += step;
  }
  return chunks;
}

async function embedInlineChunk(content: string): Promise<number[] | null> {
  if (!QUERY_EMBED_FUNCTION_NAME) return null;
  try {
    const response = await getLambdaClient().send(
      new InvokeCommand({
        FunctionName: QUERY_EMBED_FUNCTION_NAME,
        InvocationType: "RequestResponse",
        Payload: Buffer.from(JSON.stringify({ query: content }))
      })
    );
    const raw = Buffer.from(response.Payload ?? new Uint8Array()).toString("utf-8");
    if (response.FunctionError) {
      console.warn("vault create inline embedding failed", raw || response.FunctionError);
      return null;
    }
    const payload = raw ? JSON.parse(raw) : {};
    const parsed = z
      .object({
        embedding: z.array(z.number()).length(EMBEDDING_DIM),
        embedding_model: z.literal(EMBEDDING_MODEL),
        embedding_dim: z.literal(EMBEDDING_DIM)
      })
      .safeParse(payload);
    return parsed.success ? parsed.data.embedding : null;
  } catch (error) {
    console.warn("vault create inline embedding failed", error);
    return null;
  }
}

async function indexInlineContent(entry: VaultEntry, userId: string, content: string): Promise<VaultEntry> {
  const chunks = chunkText(content);
  if (chunks.length === 0) return entry;

  let embedded = 0;
  for (const [index, chunk] of chunks.entries()) {
    const chunkId = `${entry.id}:${index}`;
    const embedding = await embedInlineChunk(chunk.content);
    if (embedding) embedded += 1;
    await query(
      `INSERT INTO vault.chunks
         (id, entry_id, user_id, chunk_index, content, embedding, token_count, chunk_hash)
       VALUES ($1, $2, $3, $4, $5, $6::vector, $7, $8)
       ON CONFLICT (entry_id, chunk_index) DO NOTHING`,
      [
        chunkId,
        entry.id,
        userId,
        index,
        chunk.content,
        embedding ? vectorLiteral(embedding) : null,
        chunk.token_count,
        null
      ]
    );
    await query(
      `INSERT INTO vault.chunks_fts (content, entry_id, chunk_id, user_id)
       VALUES ($1, $2, $3, $4)`,
      [chunk.content, entry.id, chunkId, userId]
    );
  }

  const updated = await query<VaultEntry>(
    `UPDATE vault.entries
        SET chunk_count = $1,
            embedding_model = $2,
            chunker_version = $3,
            indexed_at = $4,
            updated_at = $4
      WHERE id = $5 AND user_id = $6
      RETURNING *`,
    [
      chunks.length,
      embedded > 0 ? EMBEDDING_MODEL : null,
      embedded > 0 ? CHUNKER_VERSION : null,
      Math.floor(Date.now() / 1000),
      entry.id,
      userId
    ]
  );

  return updated[0] ?? entry;
}

export async function createEntry(
  event: APIGatewayProxyEvent,
  auth: AuthContext
): Promise<APIGatewayProxyResult> {
  let body: unknown;
  try {
    body = event.body ? JSON.parse(event.body) : {};
  } catch {
    throw ApiError.invalidInput("Request body is not valid JSON");
  }
  const parsed = CreateRequest.safeParse(body);
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    const path = issue?.path?.join(".") ?? "(root)";
    throw ApiError.invalidInput(`${path}: ${issue?.message ?? "Invalid body"}`);
  }
  const input = parsed.data;

  const rows = await query<VaultEntry>(
    `INSERT INTO vault.entries (
       id, user_id, kind, subkind, title, content,
       source_path, vault_blob_path,
       scope_type, scope_project_id, memory_type,
       pinned, tags, mime, size_bytes,
       index_status, cloud_sync_state, classifier_confidence,
       hit_count, last_retrieved_at,
       created_at, updated_at, owner_user_id, project_id
     ) VALUES (
       $1, $2, $3, $4, $5, $6,
       $7, $8,
       $9, $10, $11,
       $12, $13, $14, $15,
       $16, $17, $18,
       $19, $20,
       $21, $22, $23, $24
     )
     RETURNING *`,
    [
      input.id,
      auth.user_id,
      input.kind,
      input.subkind ?? null,
      input.title,
      input.content ?? null,
      input.source_path ?? null,
      input.vault_blob_path ?? null,
      input.scope_type,
      input.scope_project_id ?? null,
      input.memory_type,
      input.pinned,
      input.tags,
      input.mime ?? null,
      input.size_bytes ?? null,
      input.index_status,
      input.cloud_sync_state,
      input.classifier_confidence,
      input.hit_count,
      input.last_retrieved_at ?? null,
      input.created_at,
      input.updated_at,
      auth.user_id,
      input.scope_project_id ?? null
    ]
  );

  let entry = rows[0];
  if (input.content?.trim() && !input.vault_blob_path) {
    entry = await indexInlineContent(entry, auth.user_id, input.content);
  }

  return created(entry);
}
