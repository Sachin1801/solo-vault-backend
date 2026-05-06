#!/usr/bin/env node
// Temporary deploy script for INFRA-3.
// Will be replaced once INFRA-6's `deploy.ts --stack <name>` refactor lands on main.
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  CloudFormationClient,
  CreateStackCommand,
  DeleteStackCommand,
  DescribeStacksCommand,
  Parameter,
  Tag,
  UpdateStackCommand,
  waitUntilStackCreateComplete,
  waitUntilStackDeleteComplete,
  waitUntilStackUpdateComplete,
} from "@aws-sdk/client-cloudformation";

type Environment = "dev" | "staging";
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
    throw new Error("Missing --env argument. Use --env dev or --env staging.");
  }
  const v = process.argv[i + 1];
  if (v !== "dev" && v !== "staging") {
    throw new Error("Invalid --env value. Allowed: dev, staging.");
  }
  return v;
}

function parseConfirmDestroyArg(expectedStackName: string): void {
  const i = process.argv.findIndex((a) => a === "--confirm-destroy");
  if (i === -1 || i + 1 >= process.argv.length) {
    throw new Error(
      `Destroy requires explicit confirmation: --confirm-destroy ${expectedStackName}`
    );
  }
  const v = process.argv[i + 1];
  if (v !== expectedStackName) {
    throw new Error(
      `Destroy confirmation mismatch. Expected --confirm-destroy ${expectedStackName}`
    );
  }
}

function loadConfig(env: Environment): BaseConfig {
  const configPath = resolve(process.cwd(), "infra", "config", `${env}.json`);
  return JSON.parse(readFileSync(configPath, "utf-8")) as BaseConfig;
}

function stackName(config: BaseConfig): string {
  return `${config.project_prefix}-api-gateway-${config.environment}`;
}

function createParameters(config: BaseConfig): Parameter[] {
  return [
    { ParameterKey: "ProjectPrefix", ParameterValue: config.project_prefix },
    { ParameterKey: "EnvironmentName", ParameterValue: config.environment },
  ];
}

function createTags(config: BaseConfig): Tag[] {
  return Object.entries(config.tags ?? {}).map(([Key, Value]) => ({ Key, Value }));
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

async function deployStack(client: CloudFormationClient, config: BaseConfig): Promise<void> {
  const templatePath = resolve(
    process.cwd(),
    "infra",
    "cloudformation",
    "api-gateway.yml"
  );
  const templateBody = readFileSync(templatePath, "utf-8");
  const name = stackName(config);
  const parameters = createParameters(config);
  const tags = createTags(config);
  const exists = await stackExists(client, name);

  if (exists) {
    try {
      console.log(`Updating stack ${name} in ${config.region}...`);
      await client.send(
        new UpdateStackCommand({ StackName: name, TemplateBody: templateBody, Parameters: parameters, Tags: tags })
      );
      await waitUntilStackUpdateComplete({ client, maxWaitTime: 600 }, { StackName: name });
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
    console.log(`Creating stack ${name} in ${config.region}...`);
    await client.send(
      new CreateStackCommand({ StackName: name, TemplateBody: templateBody, Parameters: parameters, Tags: tags })
    );
    await waitUntilStackCreateComplete({ client, maxWaitTime: 600 }, { StackName: name });
    console.log(`Stack creation complete: ${name}`);
  }

  await printOutputs(client, name);
}

async function destroyStack(client: CloudFormationClient, config: BaseConfig): Promise<void> {
  const name = stackName(config);
  const exists = await stackExists(client, name);
  if (!exists) {
    console.log(`Stack does not exist, nothing to destroy: ${name}`);
    return;
  }
  parseConfirmDestroyArg(name);
  console.log(`Deleting stack ${name} in ${config.region}...`);
  await client.send(new DeleteStackCommand({ StackName: name }));
  await waitUntilStackDeleteComplete({ client, maxWaitTime: 600 }, { StackName: name });
  console.log(`Stack deletion complete: ${name}`);
}

async function run(): Promise<void> {
  const action = parseActionArg();
  const env = parseEnvArg();
  const config = loadConfig(env);
  const client = new CloudFormationClient({ region: config.region });
  if (action === "destroy") {
    await destroyStack(client, config);
    return;
  }
  await deployStack(client, config);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
