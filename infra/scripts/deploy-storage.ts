#!/usr/bin/env node
// Deploy the vault file-storage S3 bucket CloudFormation stack.
//
// Must run before deploy:vault-crud so the bucket ARN export exists.
//
// Usage:
//   npm run deploy:storage          # dev
//   npm run deploy:storage:staging  # staging

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  CloudFormationClient,
  CreateStackCommand,
  DeleteStackCommand,
  DescribeStacksCommand,
  Tag,
  UpdateStackCommand,
  waitUntilStackCreateComplete,
  waitUntilStackDeleteComplete,
  waitUntilStackUpdateComplete,
} from "@aws-sdk/client-cloudformation";

type Environment = "dev" | "staging";
type Action = "deploy" | "destroy";

function parseActionArg(): Action {
  const i = process.argv.findIndex((a) => a === "--action");
  if (i === -1 || i + 1 >= process.argv.length) return "deploy";
  const v = process.argv[i + 1];
  if (v !== "deploy" && v !== "destroy") throw new Error("Invalid --action. Allowed: deploy, destroy.");
  return v;
}

function parseEnvArg(): Environment {
  const i = process.argv.findIndex((a) => a === "--env");
  if (i === -1 || i + 1 >= process.argv.length) throw new Error("Missing --env. Use --env dev or --env staging.");
  const v = process.argv[i + 1];
  if (v !== "dev" && v !== "staging") throw new Error("Invalid --env. Allowed: dev, staging.");
  return v;
}

function parseConfirmDestroyArg(expected: string): void {
  const i = process.argv.findIndex((a) => a === "--confirm-destroy");
  if (i === -1 || i + 1 >= process.argv.length)
    throw new Error(`Destroy requires: --confirm-destroy ${expected}`);
  if (process.argv[i + 1] !== expected)
    throw new Error(`Destroy confirmation mismatch. Expected: --confirm-destroy ${expected}`);
}

function loadConfig(env: Environment) {
  return JSON.parse(readFileSync(resolve(process.cwd(), "infra", "config", `${env}.json`), "utf-8")) as {
    project_prefix: string;
    environment: string;
    region: string;
    tags?: Record<string, string>;
  };
}

function stackName(prefix: string, env: string): string {
  return `${prefix}-storage-${env}`;
}

async function stackExists(cfn: CloudFormationClient, name: string): Promise<boolean> {
  try {
    await cfn.send(new DescribeStacksCommand({ StackName: name }));
    return true;
  } catch (err) {
    if (err instanceof Error && err.message.includes("does not exist")) return false;
    throw err;
  }
}

async function run(): Promise<void> {
  const action = parseActionArg();
  const env = parseEnvArg();
  const config = loadConfig(env);
  const cfn = new CloudFormationClient({ region: config.region });
  const name = stackName(config.project_prefix, config.environment);

  if (action === "destroy") {
    parseConfirmDestroyArg(name);
    if (!(await stackExists(cfn, name))) {
      console.log(`Stack does not exist: ${name}`);
      return;
    }
    console.log(`Deleting stack ${name}...`);
    await cfn.send(new DeleteStackCommand({ StackName: name }));
    await waitUntilStackDeleteComplete({ client: cfn, maxWaitTime: 300 }, { StackName: name });
    console.log("Done.");
    return;
  }

  const templateBody = readFileSync(
    resolve(process.cwd(), "infra", "cloudformation", "storage.yml"),
    "utf-8",
  );
  const parameters = [
    { ParameterKey: "ProjectPrefix", ParameterValue: config.project_prefix },
    { ParameterKey: "EnvironmentName", ParameterValue: config.environment },
  ];
  const tags: Tag[] = Object.entries(config.tags ?? {}).map(([Key, Value]) => ({ Key, Value }));

  const exists = await stackExists(cfn, name);
  if (exists) {
    try {
      console.log(`Updating stack ${name}...`);
      await cfn.send(new UpdateStackCommand({ StackName: name, TemplateBody: templateBody, Parameters: parameters, Tags: tags }));
      await waitUntilStackUpdateComplete({ client: cfn, maxWaitTime: 300 }, { StackName: name });
      console.log(`Stack update complete: ${name}`);
    } catch (err) {
      if (err instanceof Error && err.message.includes("No updates are to be performed")) {
        console.log(`No changes: ${name}`);
        return;
      }
      throw err;
    }
  } else {
    console.log(`Creating stack ${name}...`);
    await cfn.send(new CreateStackCommand({ StackName: name, TemplateBody: templateBody, Parameters: parameters, Tags: tags }));
    await waitUntilStackCreateComplete({ client: cfn, maxWaitTime: 300 }, { StackName: name });
    console.log(`Stack creation complete: ${name}`);
  }

  const result = await cfn.send(new DescribeStacksCommand({ StackName: name }));
  for (const o of result.Stacks?.[0]?.Outputs ?? []) {
    console.log(`  ${o.OutputKey}: ${o.OutputValue}`);
  }
}

run().catch((err) => { console.error(err); process.exit(1); });
