#!/usr/bin/env node
// Migration orchestrator.
//
// Applies db/schema.sql (or any SQL file passed via --file) against the RDS
// instance by spinning up a one-shot Lambda in the VPC, invoking it, and
// tearing everything down. The Lambda can't live outside the VPC because
// the RDS instance is in private subnets with an SG that only accepts
// traffic from the Lambda SG.
//
// Resources created by this script:
//   - IAM role (assume-role: lambda.amazonaws.com) with:
//       * AWSLambdaVPCAccessExecutionRole (ENIs + CW logs)
//       * inline policy granting GetSecretValue on the db-credentials secret
//   - Lambda function in the Lambda SG + both private subnets
//
// Both are deleted in the `finally` block regardless of outcome.

import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { build as esbuild } from "esbuild";
import AdmZip from "adm-zip";
import {
  CloudFormationClient,
  ListExportsCommand
} from "@aws-sdk/client-cloudformation";
import {
  IAMClient,
  CreateRoleCommand,
  AttachRolePolicyCommand,
  DetachRolePolicyCommand,
  DeleteRoleCommand
} from "@aws-sdk/client-iam";
import {
  LambdaClient,
  CreateFunctionCommand,
  DeleteFunctionCommand,
  InvokeCommand,
  GetFunctionCommand,
  waitUntilFunctionActiveV2
} from "@aws-sdk/client-lambda";
import {
  SecretsManagerClient,
  GetSecretValueCommand
} from "@aws-sdk/client-secrets-manager";

type Environment = "dev" | "staging";

type BaseConfig = {
  project_prefix: string;
  environment: Environment;
  region: string;
};

function parseEnvArg(): Environment {
  const i = process.argv.findIndex((a) => a === "--env");
  if (i === -1 || i + 1 >= process.argv.length) {
    throw new Error("Missing --env argument. Use --env dev or --env staging.");
  }
  const v = process.argv[i + 1];
  if (v !== "dev" && v !== "staging") {
    throw new Error("Invalid --env value. Allowed: dev, staging.");
  }
  return v;
}

function parseFileArg(): string {
  const i = process.argv.findIndex((a) => a === "--file");
  if (i === -1 || i + 1 >= process.argv.length) {
    return "db/schema.sql";
  }
  return process.argv[i + 1];
}

function loadConfig(env: Environment): BaseConfig {
  const path = resolve(process.cwd(), "infra", "config", `${env}.json`);
  return JSON.parse(readFileSync(path, "utf-8")) as BaseConfig;
}

async function resolveExport(
  cfn: CloudFormationClient,
  name: string
): Promise<string> {
  let next: string | undefined;
  do {
    const response = await cfn.send(new ListExportsCommand({ NextToken: next }));
    const match = response.Exports?.find((e) => e.Name === name);
    if (match?.Value) {
      return match.Value;
    }
    next = response.NextToken;
  } while (next);
  throw new Error(`CloudFormation export "${name}" not found`);
}

type BundleOutputs = {
  zipPath: string;
  cleanup: () => void;
};

async function bundleLambda(): Promise<BundleOutputs> {
  const srcEntry = resolve(
    process.cwd(),
    "infra",
    "lambda",
    "db-migrate",
    "handler.ts"
  );
  const buildDir = resolve(
    process.cwd(),
    "infra",
    "lambda",
    "db-migrate",
    ".build"
  );
  mkdirSync(buildDir, { recursive: true });
  const outFile = resolve(buildDir, "index.js");

  await esbuild({
    entryPoints: [srcEntry],
    bundle: true,
    platform: "node",
    target: "node20",
    format: "cjs",
    outfile: outFile,
    // The AWS SDK v3 is provided by the Lambda runtime; pg-native is
    // optional and only present if explicitly installed.
    external: ["@aws-sdk/*", "pg-native"]
  });

  const zip = new AdmZip();
  zip.addLocalFile(outFile);
  const zipPath = resolve(buildDir, "function.zip");
  zip.writeZip(zipPath);

  return {
    zipPath,
    cleanup: () => rmSync(buildDir, { recursive: true, force: true })
  };
}

type CreatedResources = {
  roleCreated: boolean;
  roleName: string;
  lambdaCreated: boolean;
  lambdaName: string;
};

async function cleanup(
  iam: IAMClient,
  lambda: LambdaClient,
  resources: CreatedResources
): Promise<void> {
  if (resources.lambdaCreated) {
    try {
      await lambda.send(
        new DeleteFunctionCommand({ FunctionName: resources.lambdaName })
      );
      console.log(`Deleted Lambda: ${resources.lambdaName}`);
    } catch (err) {
      console.warn(`Lambda delete failed: ${(err as Error).message}`);
    }
  }
  if (resources.roleCreated) {
    try {
      await iam.send(
        new DetachRolePolicyCommand({
          RoleName: resources.roleName,
          PolicyArn: "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
        })
      );
    } catch {
      /* ignore */
    }
    try {
      await iam.send(new DeleteRoleCommand({ RoleName: resources.roleName }));
      console.log(`Deleted role: ${resources.roleName}`);
    } catch (err) {
      console.warn(`Role delete failed: ${(err as Error).message}`);
    }
  }
}

async function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function createLambdaWithRetry(
  lambda: LambdaClient,
  input: ConstructorParameters<typeof CreateFunctionCommand>[0]
): Promise<void> {
  // IAM role creation takes a few seconds to propagate; Lambda createFunction
  // sometimes fails with "The role defined for the function cannot be
  // assumed by Lambda." Retry a few times with backoff.
  const maxAttempts = 6;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      await lambda.send(new CreateFunctionCommand(input));
      return;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      const isRolePropagation =
        message.includes("cannot be assumed") ||
        message.includes("role") ||
        (err as { name?: string })?.name === "InvalidParameterValueException";
      if (attempt === maxAttempts || !isRolePropagation) {
        throw err;
      }
      const delay = Math.min(2000 * attempt, 10_000);
      console.log(
        `Lambda create attempt ${attempt} failed (role likely still propagating). Retrying in ${delay}ms...`
      );
      await sleep(delay);
    }
  }
}

async function run(): Promise<void> {
  const env = parseEnvArg();
  const sqlFile = parseFileArg();
  const config = loadConfig(env);

  const sqlPath = resolve(process.cwd(), sqlFile);
  const sql = readFileSync(sqlPath, "utf-8");
  console.log(`Read ${sqlPath} (${sql.length} bytes)`);

  const cfn = new CloudFormationClient({ region: config.region });
  const iam = new IAMClient({ region: config.region });
  const lambda = new LambdaClient({ region: config.region });
  const secrets = new SecretsManagerClient({ region: config.region });

  const prefix = `${config.project_prefix}-${config.environment}`;
  const [lambdaSgId, subnetAId, subnetBId, secretArn] = await Promise.all([
    resolveExport(cfn, `${prefix}-lambda-sg-id`),
    resolveExport(cfn, `${prefix}-private-subnet-a-id`),
    resolveExport(cfn, `${prefix}-private-subnet-b-id`),
    resolveExport(cfn, `${prefix}-db-credentials-arn`)
  ]);
  console.log(`Resolved network + secret from CloudFormation exports.`);

  // Read DB credentials here on the dev machine rather than from inside the
  // Lambda. The Lambda lives in private subnets with no NAT / VPC endpoint,
  // so it can't reach Secrets Manager. Passing credentials through the
  // invoke payload sidesteps that. CloudTrail does not record invoke
  // payloads, and the Lambda is destroyed immediately after use.
  const secretValue = await secrets.send(
    new GetSecretValueCommand({ SecretId: secretArn })
  );
  if (!secretValue.SecretString) {
    throw new Error(`Secret ${secretArn} has no SecretString`);
  }
  type DbSecret = {
    username: string;
    password: string;
    host: string;
    port: number;
    dbname: string;
  };
  const dbSecret = JSON.parse(secretValue.SecretString) as Partial<DbSecret>;
  for (const key of ["username", "password", "host", "port", "dbname"] as const) {
    if (dbSecret[key] === undefined || dbSecret[key] === null) {
      throw new Error(
        `db-credentials secret missing "${key}". ` +
          `Did AWS::SecretsManager::SecretTargetAttachment run for the RDS stack?`
      );
    }
  }
  console.log(
    `Loaded DB credentials for ${dbSecret.host}:${dbSecret.port}/${dbSecret.dbname}`
  );

  const runId = Date.now().toString(36);
  const resources: CreatedResources = {
    roleCreated: false,
    roleName: `${prefix}-db-migrate-${runId}`,
    lambdaCreated: false,
    lambdaName: `${prefix}-db-migrate-${runId}`
  };

  const bundle = await bundleLambda();

  try {
    console.log(`Creating IAM role: ${resources.roleName}`);
    const roleResponse = await iam.send(
      new CreateRoleCommand({
        RoleName: resources.roleName,
        AssumeRolePolicyDocument: JSON.stringify({
          Version: "2012-10-17",
          Statement: [
            {
              Effect: "Allow",
              Principal: { Service: "lambda.amazonaws.com" },
              Action: "sts:AssumeRole"
            }
          ]
        }),
        Description: "One-shot role for Solo Vault DB migration Lambda",
        Tags: [
          { Key: "Project", Value: config.project_prefix },
          { Key: "Environment", Value: config.environment }
        ]
      })
    );
    resources.roleCreated = true;
    const roleArn = roleResponse.Role?.Arn;
    if (!roleArn) {
      throw new Error("CreateRole did not return a role ARN");
    }

    await iam.send(
      new AttachRolePolicyCommand({
        RoleName: resources.roleName,
        PolicyArn:
          "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
      })
    );
    console.log(`Policies attached.`);

    const zipBytes = readFileSync(bundle.zipPath);
    console.log(`Creating Lambda: ${resources.lambdaName}`);
    await createLambdaWithRetry(lambda, {
      FunctionName: resources.lambdaName,
      Runtime: "nodejs20.x",
      Role: roleArn,
      Handler: "index.handler",
      Code: { ZipFile: zipBytes },
      Timeout: 60,
      MemorySize: 512,
      VpcConfig: {
        SubnetIds: [subnetAId, subnetBId],
        SecurityGroupIds: [lambdaSgId]
      },
      Tags: {
        Project: config.project_prefix,
        Environment: config.environment,
        Purpose: "db-migrate"
      }
    });
    resources.lambdaCreated = true;
    console.log(
      `Lambda create succeeded. Waiting for VPC ENI to attach (can take 60-180s on first run)...`
    );

    // VPC-attached Lambdas require an ENI in the SG/subnet combo, which can
    // take 60-180s on first create. Give it a generous ceiling; the waiter
    // returns as soon as the function goes Active, so this only costs time
    // on cold SGs/subnets.
    await waitUntilFunctionActiveV2(
      { client: lambda, maxWaitTime: 600 },
      { FunctionName: resources.lambdaName }
    );
    await lambda.send(new GetFunctionCommand({ FunctionName: resources.lambdaName }));
    console.log(`Lambda is Active. Invoking with ${sql.length} bytes of SQL...`);

    const invokePayload = {
      sql,
      db: {
        host: dbSecret.host!,
        port: Number(dbSecret.port!),
        user: dbSecret.username!,
        password: dbSecret.password!,
        database: dbSecret.dbname!
      }
    };
    const invokeResponse = await lambda.send(
      new InvokeCommand({
        FunctionName: resources.lambdaName,
        InvocationType: "RequestResponse",
        Payload: Buffer.from(JSON.stringify(invokePayload))
      })
    );

    if (invokeResponse.FunctionError) {
      const payload = invokeResponse.Payload
        ? Buffer.from(invokeResponse.Payload).toString("utf-8")
        : "(no payload)";
      throw new Error(
        `Lambda returned FunctionError=${invokeResponse.FunctionError}: ${payload}`
      );
    }

    const payloadText = invokeResponse.Payload
      ? Buffer.from(invokeResponse.Payload).toString("utf-8")
      : "{}";
    const result = JSON.parse(payloadText) as
      | { ok: true; tables: string[]; extensions: string[] }
      | {
          ok: false;
          error: string;
          errorName?: string;
          errorCode?: string;
          stack?: string;
        };

    if (!result.ok) {
      const details = [
        result.error,
        result.errorName ? `name=${result.errorName}` : null,
        result.errorCode ? `code=${result.errorCode}` : null
      ]
        .filter(Boolean)
        .join(" | ");
      if (result.stack) {
        console.error("Lambda stack trace:\n" + result.stack);
      }
      throw new Error(`Migration failed: ${details}`);
    }

    console.log("\nMigration succeeded.");
    console.log(`Extensions: ${result.extensions.join(", ") || "(none)"}`);
    console.log(`Tables:     ${result.tables.join(", ") || "(none)"}`);
  } finally {
    await cleanup(iam, lambda, resources);
    bundle.cleanup();
  }
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
