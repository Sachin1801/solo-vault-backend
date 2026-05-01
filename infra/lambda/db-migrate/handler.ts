// db-migrate Lambda handler.
//
// Runs a SQL blob against the Solo Vault RDS instance. Called once per
// migration by infra/scripts/migrate.ts, which deploys this Lambda, invokes
// it synchronously, then tears it down.
//
// DB credentials come in the invoke payload. We don't call Secrets Manager
// from inside the Lambda because the VPC has no outbound path to the public
// AWS API (no NAT Gateway, no VPC endpoints). The orchestrator on the dev
// machine reads the secret and passes credentials through directly.
//
// One side effect of no outbound: CloudWatch Logs doesn't ship either. So
// all diagnostic output has to come back via the return value, not logs.
// Error responses include name + code + stack to make that workable.

import pg from "pg";

type Event = {
  sql: string;
  db: {
    host: string;
    port: number;
    user: string;
    password: string;
    database: string;
  };
};

type SuccessResult = {
  ok: true;
  tables: string[];
  extensions: string[];
};

type ErrorResult = {
  ok: false;
  error: string;
  errorName?: string;
  errorCode?: string;
  stack?: string;
};

type Result = SuccessResult | ErrorResult;

function toErrorResult(err: unknown): ErrorResult {
  if (err instanceof Error) {
    return {
      ok: false,
      error: err.message || "(empty Error.message)",
      errorName: err.name,
      errorCode: (err as { code?: string }).code,
      stack: err.stack
    };
  }
  return { ok: false, error: `non-Error thrown: ${String(err)}` };
}

function validateEvent(event: unknown): ErrorResult | null {
  if (!event || typeof event !== "object") {
    return { ok: false, error: "event must be an object" };
  }
  const e = event as Partial<Event>;
  if (typeof e.sql !== "string" || e.sql.length === 0) {
    return { ok: false, error: "event.sql must be a non-empty string" };
  }
  if (!e.db || typeof e.db !== "object") {
    return { ok: false, error: "event.db is required" };
  }
  const required: (keyof Event["db"])[] = [
    "host",
    "port",
    "user",
    "password",
    "database"
  ];
  for (const key of required) {
    if (e.db[key] === undefined || e.db[key] === null || e.db[key] === "") {
      return { ok: false, error: `event.db.${key} is required` };
    }
  }
  return null;
}

export async function handler(event: Event): Promise<Result> {
  const validationError = validateEvent(event);
  if (validationError) {
    return validationError;
  }

  let client: pg.Client | null = null;
  try {
    client = new pg.Client({
      host: event.db.host,
      port: event.db.port,
      user: event.db.user,
      password: event.db.password,
      database: event.db.database,
      // RDS accepts SSL; don't verify cert because the root CA rotates and
      // traffic stays inside the VPC anyway. Not a MITM risk.
      ssl: { rejectUnauthorized: false },
      connectionTimeoutMillis: 10_000
    });
    await client.connect();

    await client.query("BEGIN");
    try {
      await client.query(event.sql);
      await client.query("COMMIT");
    } catch (err) {
      await client.query("ROLLBACK");
      throw err;
    }

    const tables = await client.query<{ tablename: string }>(
      `SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename`
    );
    const extensions = await client.query<{ extname: string }>(
      `SELECT extname FROM pg_extension ORDER BY extname`
    );
    return {
      ok: true,
      tables: tables.rows.map((r) => r.tablename),
      extensions: extensions.rows.map((r) => r.extname)
    };
  } catch (err) {
    return toErrorResult(err);
  } finally {
    if (client) {
      await client.end().catch(() => {});
    }
  }
}
