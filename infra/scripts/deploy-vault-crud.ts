#!/usr/bin/env node
// Build, upload, and deploy the vault-crud Lambda + CloudFormation stack.
//
// Usage:
//   npm run deploy:vault-crud          # dev
//   npm run deploy:vault-crud:staging  # staging
//   npm run destroy:vault-crud         # dev (requires --confirm-destroy <stack-name>)

import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { build as esbuild } from "esbuild";
import AdmZip from "adm-zip";
import {
  CloudFormationClient,
  CreateStackCommand,
  DeleteStackCommand,
  DescribeStacksCommand,
  ListExportsCommand,
  Parameter,
  Tag,
  UpdateStackCommand,
  waitUntilStackCreateComplete,
  waitUntilStackDeleteComplete,
  waitUntilStackUpdateComplete,
} from "@aws-sdk/client-cloudformation";
import {
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";

type Environment = "dev" | "staging" | "prod";
type Action = "deploy" | "destroy";

type BaseConfig = {
  project_prefix: string;
  environment: Environment;
  region: string;
  tags?: Record<string, string>;
};

function parseActionArg(): Action {
  const i = process.argv.findIndex((a) => a === "--action");
  if (i === -1 || i + 1 >= process.argv.length) return "deploy";
  const v = process.argv[i + 1];
  if (v !== "deploy" && v !== "destroy") {
    throw new Error("Invalid --action value. Allowed: deploy, destroy.");
  }
  return v;
}

function parseEnvArg(): Environment {
  const i = process.argv.findIndex((a) => a === "--env");
  if (i === -1 || i + 1 >= process.argv.length) {
    throw new Error("Missing --env argument. Use --env dev, --env staging, or --env prod.");
  }
  const v = process.argv[i + 1];
  if (v !== "dev" && v !== "staging" && v !== "prod") {
    throw new Error("Invalid --env value. Allowed: dev, staging, prod.");
  }
  return v;
}

function parseConfirmDestroyArg(expectedStackName: string): void {
  const i = process.argv.findIndex((a) => a === "--confirm-destroy");
  if (i === -1 || i + 1 >= process.argv.length) {
    throw new Error(
      `Destroy requires explicit confirmation: --confirm-destroy ${expectedStackName}`,
    );
  }
  const v = process.argv[i + 1];
  if (v !== expectedStackName) {
    throw new Error(
      `Destroy confirmation mismatch. Expected --confirm-destroy ${expectedStackName}`,
    );
  }
}

function loadConfig(env: Environment): BaseConfig {
  const configPath = resolve(process.cwd(), "infra", "config", `${env}.json`);
  return JSON.parse(readFileSync(configPath, "utf-8")) as BaseConfig;
}

function stackName(config: BaseConfig): string {
  return `${config.project_prefix}-vault-crud-lambda-${config.environment}`;
}

function artifactKey(zipBytes: Buffer): string {
  const hash = createHash("sha256").update(zipBytes).digest("hex").slice(0, 16);
  return `vault-crud/function-${hash}.zip`;
}

async function resolveExport(
  cfn: CloudFormationClient,
  name: string,
): Promise<string> {
  let next: string | undefined;
  do {
    const response = await cfn.send(new ListExportsCommand({ NextToken: next }));
    const match = response.Exports?.find((e) => e.Name === name);
    if (match?.Value) return match.Value;
    next = response.NextToken;
  } while (next);
  throw new Error(`CloudFormation export "${name}" not found`);
}

async function bundleVaultCrud(): Promise<{ zipPath: string; cleanup: () => void }> {
  const entryPoint = resolve(
    process.cwd(),
    "infra",
    "lambda",
    "vault-crud",
    "index.ts",
  );
  const buildDir = resolve(
    process.cwd(),
    "infra",
    "lambda",
    "vault-crud",
    ".build",
  );
  mkdirSync(buildDir, { recursive: true });
  const outFile = resolve(buildDir, "index.js");

  await esbuild({
    entryPoints: [entryPoint],
    bundle: true,
    platform: "node",
    target: "node20",
    format: "cjs",
    outfile: outFile,
    // Client packages are in the Lambda nodejs20.x runtime; s3-request-presigner is not, so it gets bundled.
    external: ["@aws-sdk/client-s3", "@aws-sdk/client-secrets-manager", "pg-native"],
  });

  const zip = new AdmZip();
  zip.addLocalFile(outFile);
  const zipPath = resolve(buildDir, "function.zip");
  zip.writeZip(zipPath);

  return {
    zipPath,
    cleanup: () => rmSync(buildDir, { recursive: true, force: true }),
  };
}

async function stackExists(client: CloudFormationClient, name: string): Promise<boolean> {
  try {
    await client.send(new DescribeStacksCommand({ StackName: name }));
    return true;
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes("does not exist")) return false;
    throw err;
  }
}

async function printOutputs(client: CloudFormationClient, name: string): Promise<void> {
  const result = await client.send(new DescribeStacksCommand({ StackName: name }));
  const outputs = result.Stacks?.[0]?.Outputs ?? [];
  if (outputs.length === 0) return;
  console.log("Stack outputs:");
  for (const o of outputs) {
    console.log(`  ${o.OutputKey}: ${o.OutputValue}`);
  }
}

async function deployStack(
  cfn: CloudFormationClient,
  config: BaseConfig,
  artifactsBucket: string,
  vaultCrudArtifactKey: string,
): Promise<void> {
  const templatePath = resolve(
    process.cwd(),
    "infra",
    "cloudformation",
    "lambdas.yml",
  );
  const templateBody = readFileSync(templatePath, "utf-8");
  const name = stackName(config);

  const parameters: Parameter[] = [
    { ParameterKey: "ProjectPrefix", ParameterValue: config.project_prefix },
    { ParameterKey: "EnvironmentName", ParameterValue: config.environment },
    { ParameterKey: "ArtifactsBucket", ParameterValue: artifactsBucket },
    { ParameterKey: "VaultCrudArtifactKey", ParameterValue: vaultCrudArtifactKey },
  ];
  const tags: Tag[] = Object.entries(config.tags ?? {}).map(([Key, Value]) => ({
    Key,
    Value,
  }));

  const exists = await stackExists(cfn, name);
  if (exists) {
    try {
      console.log(`Updating stack ${name}...`);
      await cfn.send(
        new UpdateStackCommand({
          StackName: name,
          TemplateBody: templateBody,
          Parameters: parameters,
          Tags: tags,
          Capabilities: ["CAPABILITY_NAMED_IAM"],
        }),
      );
      await waitUntilStackUpdateComplete({ client: cfn, maxWaitTime: 600 }, { StackName: name });
      console.log(`Stack update complete: ${name}`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("No updates are to be performed")) {
        console.log(`No changes detected for stack: ${name}`);
        return;
      }
      throw err;
    }
  } else {
    console.log(`Creating stack ${name}...`);
    await cfn.send(
      new CreateStackCommand({
        StackName: name,
        TemplateBody: templateBody,
        Parameters: parameters,
        Tags: tags,
        Capabilities: ["CAPABILITY_NAMED_IAM"],
      }),
    );
    await waitUntilStackCreateComplete({ client: cfn, maxWaitTime: 600 }, { StackName: name });
    console.log(`Stack creation complete: ${name}`);
  }

  await printOutputs(cfn, name);
}

async function destroyStack(cfn: CloudFormationClient, config: BaseConfig): Promise<void> {
  const name = stackName(config);
  const exists = await stackExists(cfn, name);
  if (!exists) {
    console.log(`Stack does not exist, nothing to destroy: ${name}`);
    return;
  }
  parseConfirmDestroyArg(name);
  console.log(`Deleting stack ${name}...`);
  await cfn.send(new DeleteStackCommand({ StackName: name }));
  await waitUntilStackDeleteComplete({ client: cfn, maxWaitTime: 600 }, { StackName: name });
  console.log(`Stack deletion complete: ${name}`);
}

async function run(): Promise<void> {
  const action = parseActionArg();
  const env = parseEnvArg();
  const config = loadConfig(env);
  const cfn = new CloudFormationClient({ region: config.region });

  if (action === "destroy") {
    await destroyStack(cfn, config);
    return;
  }

  const prefix = `${config.project_prefix}-${config.environment}`;
  const artifactsBucket = await resolveExport(cfn, `${prefix}-lambda-artifacts-bucket`);
  console.log(`Artifacts bucket: ${artifactsBucket}`);

  // Build and upload
  console.log("Building vault-crud Lambda...");
  const { zipPath, cleanup } = await bundleVaultCrud();
  try {
    const zipBytes = readFileSync(zipPath);
    const key = artifactKey(zipBytes);
    const s3 = new S3Client({ region: config.region });
    console.log(`Uploading ${key} (${zipBytes.length} bytes) to ${artifactsBucket}...`);
    await s3.send(
      new PutObjectCommand({
        Bucket: artifactsBucket,
        Key: key,
        Body: zipBytes,
        ContentType: "application/zip",
      }),
    );
    console.log("Upload complete.");

    // Deploy CloudFormation
    await deployStack(cfn, config, artifactsBucket, key);
  } finally {
    cleanup();
  }
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
