import { GetSecretValueCommand, SecretsManagerClient } from "@aws-sdk/client-secrets-manager";
import { Pool } from "pg";

type DbSecret = {
  host: string;
  port: number;
  dbname: string;
  username: string;
  password: string;
};

// Shared Secrets Manager client. Region is picked up from the Lambda
// execution environment automatically.
const secretsClient = new SecretsManagerClient({});

// Module-level pool: reused across warm Lambda invocations.
let pool: Pool | undefined;

async function loadCredentials(): Promise<DbSecret> {
  const secretId = process.env.DB_SECRET_ID;
  if (!secretId) {
    throw new Error("DB_SECRET_ID environment variable is not set");
  }
  const result = await secretsClient.send(
    new GetSecretValueCommand({ SecretId: secretId }),
  );
  if (!result.SecretString) {
    throw new Error(`Secret ${secretId} has no SecretString`);
  }
  return JSON.parse(result.SecretString) as DbSecret;
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
    // Lambda concurrency × max = max DB connections. db.t3.micro allows ~87
    // connections; max=3 leaves headroom for pipeline + future Lambdas.
    max: 3,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 5_000,
    ssl: { rejectUnauthorized: false },
  });
  return pool;
}

export async function query<T = Record<string, unknown>>(
  text: string,
  params: unknown[] = [],
): Promise<T[]> {
  const p = await getPool();
  const result = await p.query(text, params);
  return result.rows as T[];
}

// Upsert the user row on every authenticated request so the users table
// stays in sync with Cognito without a separate provisioning step.
export async function ensureUser(userId: string, email: string): Promise<void> {
  await query(
    `INSERT INTO users (id, email) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING`,
    [userId, email],
  );
}
