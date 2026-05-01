import { Pool } from "pg";
import {
  SecretsManagerClient,
  GetSecretValueCommand
} from "@aws-sdk/client-secrets-manager";

interface DbCredentials {
  host: string;
  port: number;
  dbname: string;
  username: string;
  password: string;
}

// Module-level singletons survive across warm Lambda invocations. Cold start
// pays the secret fetch + pool init; warm invocations reuse both.
let pool: Pool | undefined;
const secrets = new SecretsManagerClient({});

async function loadCredentials(): Promise<DbCredentials> {
  const secretId = process.env.DB_SECRET_ID;
  if (!secretId) {
    throw new Error("DB_SECRET_ID environment variable is not set");
  }
  const result = await secrets.send(
    new GetSecretValueCommand({ SecretId: secretId })
  );
  if (!result.SecretString) {
    throw new Error(`Secret ${secretId} has no SecretString`);
  }
  return JSON.parse(result.SecretString) as DbCredentials;
}

export async function getPool(): Promise<Pool> {
  if (pool) return pool;
  const creds = await loadCredentials();
  pool = new Pool({
    host: creds.host,
    port: creds.port,
    database: creds.dbname,
    user: creds.username,
    password: creds.password,
    // Lambda concurrency × max = max DB connections. db.t3.micro caps around
    // 87 connections; conservative max=3 leaves headroom for pipeline + future
    // Lambdas. Bump if we ever see "too many connections" in CloudWatch.
    max: 3,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 5_000,
    ssl: { rejectUnauthorized: false }
  });
  return pool;
}

export async function query<T>(
  text: string,
  params: readonly unknown[] = []
): Promise<T[]> {
  const p = await getPool();
  const result = await p.query(text, params as unknown[]);
  return result.rows as T[];
}

// Lazy-create the user row on first request. The auth-handler Lambda (API-1)
// is supposed to do this on /auth/link, but we can't depend on it being
// called first — so insert-on-conflict here is cheap and safe.
export async function ensureUser(
  userId: string,
  email: string | undefined
): Promise<void> {
  // Cognito always issues an email for our user pool, but be defensive in
  // case the claim is missing — fall back to a placeholder so the NOT NULL
  // constraint doesn't trip.
  const safeEmail = email ?? `${userId}@unknown.local`;
  await query(
    `INSERT INTO users (id, email) VALUES ($1, $2)
       ON CONFLICT (id) DO NOTHING`,
    [userId, safeEmail]
  );
}
