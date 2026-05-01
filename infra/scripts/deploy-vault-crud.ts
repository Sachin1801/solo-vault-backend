#!/usr/bin/env node
// Deploys the vault-crud Lambda (API-2):
//   1. Bundles infra/lambda/vault-crud + shared utilities into one zip
//   2. Uploads to the lambda-artifacts S3 bucket under a content-hashed key
//   3. Creates or updates the vault-crud-lambda CloudFormation stack with
//      that S3 key as ArtifactsKey
//   4. Patches the api-gateway stack: passes the new Lambda ARN so the
//      5 /vault/entries methods switch from MOCK to AWS_PROXY
//
// Step 4 only updates the VaultCrudLambdaArn parameter; everything else on
// the api-gateway stack uses UsePreviousValue, so this won't reset other
// params (StageName, throttle limits) to defaults.

import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { build as esbuild } from "esbuild";
import AdmZip from "adm-zip";
import {
  CloudFormationClient,
  CreateStackCommand,
  DescribeStacksCommand,
  ListExportsCommand,
  UpdateStackCommand,
  waitUntilStackCreateComplete,
  waitUntilStackUpdateComplete
} from "@aws-sdk/client-cloudformation";
import { S3Client, PutObjectCommand } from "@aws-sdk/client-s3";
import {
  APIGatewayClient,
  CreateDeploymentCommand
} from "@aws-sdk/client-api-gateway";

type Environment = "dev" | "staging";
type BaseConfig = {
  project_prefix: string;
  environment: Environment;
  region: string;
  tags?: Record<string, string>;
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
    if (match?.Value) return match.Value;
    next = response.NextToken;
  } while (next);
  throw new Error(`CloudFormation export "${name}" not found`);
}

async function bundleLambda(): Promise<{ zipPath: string; hash: string; cleanup: () => void }> {
  const srcEntry = resolve(
    process.cwd(),
    "infra",
    "lambda",
    "vault-crud",
    "index.ts"
  );
  const buildDir = resolve(
    process.cwd(),
    "infra",
    "lambda",
    "vault-crud",
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
    external: ["@aws-sdk/*", "pg-native"]
  });

  const zip = new AdmZip();
  zip.addLocalFile(outFile);
  const zipPath = resolve(buildDir, "function.zip");
  zip.writeZip(zipPath);

  // Hash the zip bytes so each unique build gets a unique S3 key. CFN sees
  // a real change in ArtifactsKey and updates the Lambda code.
  const bytes = readFileSync(zipPath);
  const hash = createHash("sha256").update(bytes).digest("hex").slice(0, 16);

  return {
    zipPath,
    hash,
    cleanup: () => rmSync(buildDir, { recursive: true, force: true })
  };
}

async function stackExists(
  cfn: CloudFormationClient,
  stackName: string
): Promise<boolean> {
  try {
    await cfn.send(new DescribeStacksCommand({ StackName: stackName }));
    return true;
  } catch (err) {
    if (err instanceof Error && /does not exist/i.test(err.message)) {
      return false;
    }
    throw err;
  }
}

async function getStackOutput(
  cfn: CloudFormationClient,
  stackName: string,
  outputKey: string
): Promise<string> {
  const response = await cfn.send(
    new DescribeStacksCommand({ StackName: stackName })
  );
  const output = response.Stacks?.[0]?.Outputs?.find(
    (o) => o.OutputKey === outputKey
  );
  if (!output?.OutputValue) {
    throw new Error(
      `Stack ${stackName} has no output named ${outputKey}`
    );
  }
  return output.OutputValue;
}

async function deployLambdaStack(
  cfn: CloudFormationClient,
  config: BaseConfig,
  artifactsKey: string
): Promise<string> {
  const stackName = `${config.project_prefix}-vault-crud-lambda-${config.environment}`;
  const templateBody = readFileSync(
    resolve(process.cwd(), "infra", "cloudformation", "vault-crud-lambda.yml"),
    "utf-8"
  );
  const parameters = [
    { ParameterKey: "ProjectPrefix", ParameterValue: config.project_prefix },
    { ParameterKey: "EnvironmentName", ParameterValue: config.environment },
    { ParameterKey: "ArtifactsKey", ParameterValue: artifactsKey }
  ];
  const tags = Object.entries(config.tags ?? {}).map(([Key, Value]) => ({
    Key,
    Value
  }));
  const exists = await stackExists(cfn, stackName);

  if (exists) {
    try {
      console.log(`Updating ${stackName}...`);
      await cfn.send(
        new UpdateStackCommand({
          StackName: stackName,
          TemplateBody: templateBody,
          Parameters: parameters,
          Capabilities: ["CAPABILITY_NAMED_IAM"],
          Tags: tags
        })
      );
      await waitUntilStackUpdateComplete(
        { client: cfn, maxWaitTime: 600 },
        { StackName: stackName }
      );
    } catch (err) {
      if (err instanceof Error && /No updates are to be performed/i.test(err.message)) {
        console.log(`No changes for ${stackName}.`);
      } else {
        throw err;
      }
    }
  } else {
    console.log(`Creating ${stackName}...`);
    await cfn.send(
      new CreateStackCommand({
        StackName: stackName,
        TemplateBody: templateBody,
        Parameters: parameters,
        Capabilities: ["CAPABILITY_NAMED_IAM"],
        Tags: tags
      })
    );
    await waitUntilStackCreateComplete(
      { client: cfn, maxWaitTime: 600 },
      { StackName: stackName }
    );
  }

  return getStackOutput(cfn, stackName, "FunctionArn");
}

async function patchApiGateway(
  cfn: CloudFormationClient,
  apigw: APIGatewayClient,
  config: BaseConfig,
  lambdaArn: string
): Promise<void> {
  const stackName = `${config.project_prefix}-api-gateway-${config.environment}`;
  const templateBody = readFileSync(
    resolve(process.cwd(), "infra", "cloudformation", "api-gateway.yml"),
    "utf-8"
  );

  // Pull the existing parameter list and reuse all values except
  // VaultCrudLambdaArn — which we set to the new ARN.
  const existing = await cfn.send(
    new DescribeStacksCommand({ StackName: stackName })
  );
  const existingParams = existing.Stacks?.[0]?.Parameters ?? [];
  const parameters = existingParams.map((p) => {
    if (p.ParameterKey === "VaultCrudLambdaArn") {
      return { ParameterKey: "VaultCrudLambdaArn", ParameterValue: lambdaArn };
    }
    return { ParameterKey: p.ParameterKey, UsePreviousValue: true };
  });
  // If the existing stack predates our parameter (deployed before this PR),
  // there's no entry for VaultCrudLambdaArn in existingParams — append it.
  if (!existingParams.some((p) => p.ParameterKey === "VaultCrudLambdaArn")) {
    parameters.push({
      ParameterKey: "VaultCrudLambdaArn",
      ParameterValue: lambdaArn
    });
  }

  try {
    console.log(`Patching ${stackName} with vault-crud Lambda ARN...`);
    await cfn.send(
      new UpdateStackCommand({
        StackName: stackName,
        TemplateBody: templateBody,
        Parameters: parameters
      })
    );
    await waitUntilStackUpdateComplete(
      { client: cfn, maxWaitTime: 600 },
      { StackName: stackName }
    );
    console.log(`api-gateway updated.`);
  } catch (err) {
    if (err instanceof Error && /No updates are to be performed/i.test(err.message)) {
      console.log(`No changes for ${stackName}.`);
    } else {
      throw err;
    }
  }

  // CFN updates the Method/Integration resources, but the live stage keeps
  // serving the previous Deployment snapshot until something explicitly
  // creates a new one. Force it here so the new AWS_PROXY routes go live.
  const apiId = existing.Stacks?.[0]?.Outputs?.find(
    (o) => o.OutputKey === "ApiId"
  )?.OutputValue;
  const stageName = existingParams.find(
    (p) => p.ParameterKey === "StageName"
  )?.ParameterValue;
  if (!apiId || !stageName) {
    throw new Error(
      `Could not resolve ApiId/StageName from ${stackName} for redeploy`
    );
  }
  console.log(`Forcing new APIGW deployment on ${apiId}:${stageName}...`);
  await apigw.send(
    new CreateDeploymentCommand({
      restApiId: apiId,
      stageName,
      description: `vault-crud deploy: pick up new method integrations`
    })
  );
}

async function run(): Promise<void> {
  const env = parseEnvArg();
  const config = loadConfig(env);
  const cfn = new CloudFormationClient({ region: config.region });
  const s3 = new S3Client({ region: config.region });
  const apigw = new APIGatewayClient({ region: config.region });

  const prefix = `${config.project_prefix}-${config.environment}`;
  const bucketName = await resolveExport(
    cfn,
    `${prefix}-lambda-artifacts-bucket`
  );
  console.log(`Artifacts bucket: ${bucketName}`);

  const bundle = await bundleLambda();
  try {
    const s3Key = `vault-crud/${bundle.hash}.zip`;
    console.log(`Uploading zip → s3://${bucketName}/${s3Key}`);
    await s3.send(
      new PutObjectCommand({
        Bucket: bucketName,
        Key: s3Key,
        Body: readFileSync(bundle.zipPath),
        ContentType: "application/zip"
      })
    );

    const lambdaArn = await deployLambdaStack(cfn, config, s3Key);
    console.log(`vault-crud Lambda ARN: ${lambdaArn}`);

    await patchApiGateway(cfn, apigw, config, lambdaArn);

    console.log("\nDone. /vault/entries methods are now wired to the Lambda.");
  } finally {
    bundle.cleanup();
  }
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
