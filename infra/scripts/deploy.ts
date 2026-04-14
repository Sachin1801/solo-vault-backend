#!/usr/bin/env node
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
  waitUntilStackUpdateComplete
} from "@aws-sdk/client-cloudformation";

type Environment = "dev" | "staging";

type DeployConfig = {
  project_prefix: string;
  environment: Environment;
  region: string;
  stack_name: string;
  vpc_cidr: string;
  private_subnet_a_cidr: string;
  private_subnet_b_cidr: string;
  tags?: Record<string, string>;
};

type Action = "deploy" | "destroy";

function parseConfirmDestroyArg(expectedStackName: string): void {
  const confirmIndex = process.argv.findIndex((arg) => arg === "--confirm-destroy");
  if (confirmIndex === -1 || confirmIndex + 1 >= process.argv.length) {
    throw new Error(
      `Destroy requires explicit confirmation: --confirm-destroy ${expectedStackName}`
    );
  }
  const confirmValue = process.argv[confirmIndex + 1];
  if (confirmValue !== expectedStackName) {
    throw new Error(
      `Destroy confirmation mismatch. Expected --confirm-destroy ${expectedStackName}`
    );
  }
}

function parseActionArg(): Action {
  const actionIndex = process.argv.findIndex((arg) => arg === "--action");
  if (actionIndex === -1 || actionIndex + 1 >= process.argv.length) {
    return "deploy";
  }
  const actionValue = process.argv[actionIndex + 1];
  if (actionValue !== "deploy" && actionValue !== "destroy") {
    throw new Error("Invalid --action value. Allowed: deploy, destroy.");
  }
  return actionValue;
}

function parseEnvArg(): Environment {
  const envIndex = process.argv.findIndex((arg) => arg === "--env");
  if (envIndex === -1 || envIndex + 1 >= process.argv.length) {
    throw new Error("Missing --env argument. Use --env dev or --env staging.");
  }

  const envValue = process.argv[envIndex + 1];
  if (envValue !== "dev" && envValue !== "staging") {
    throw new Error("Invalid --env value. Allowed: dev, staging.");
  }

  return envValue;
}

function loadConfig(env: Environment): DeployConfig {
  const configPath = resolve(process.cwd(), "infra", "config", `${env}.json`);
  const raw = readFileSync(configPath, "utf-8");
  const config = JSON.parse(raw) as DeployConfig;

  if (config.environment !== env) {
    throw new Error(
      `Config mismatch: ${configPath} declares environment "${config.environment}" but CLI selected "${env}". ` +
        `Refusing to deploy to avoid targeting the wrong stack.`
    );
  }

  return config;
}

function createParameters(config: DeployConfig): Parameter[] {
  return [
    { ParameterKey: "ProjectPrefix", ParameterValue: config.project_prefix },
    { ParameterKey: "EnvironmentName", ParameterValue: config.environment },
    { ParameterKey: "VpcCidr", ParameterValue: config.vpc_cidr },
    { ParameterKey: "PrivateSubnetACidr", ParameterValue: config.private_subnet_a_cidr },
    { ParameterKey: "PrivateSubnetBCidr", ParameterValue: config.private_subnet_b_cidr }
  ];
}

function createTags(config: DeployConfig): Tag[] {
  const entries = Object.entries(config.tags ?? {});
  return entries.map(([Key, Value]) => ({ Key, Value }));
}

// CloudFormation surfaces multiple distinct conditions as a generic `ValidationError`
// (missing stack, no-op update, bad parameter, etc.), so narrow by both the exception
// name AND a message pattern specific to the case we want to handle.
function isCloudFormationValidationError(error: unknown, messagePattern: RegExp): boolean {
  if (!(error instanceof Error)) {
    return false;
  }
  if (error.name !== "ValidationError") {
    return false;
  }
  return messagePattern.test(error.message);
}

const STACK_NOT_FOUND_PATTERN = /does not exist/i;
const NO_UPDATES_PATTERN = /no updates are to be performed/i;

async function stackExists(client: CloudFormationClient, stackName: string): Promise<boolean> {
  try {
    await client.send(new DescribeStacksCommand({ StackName: stackName }));
    return true;
  } catch (error) {
    if (isCloudFormationValidationError(error, STACK_NOT_FOUND_PATTERN)) {
      return false;
    }
    throw error;
  }
}

async function printOutputs(client: CloudFormationClient, stackName: string): Promise<void> {
  const result = await client.send(new DescribeStacksCommand({ StackName: stackName }));
  const stack = result.Stacks?.[0];
  const outputs = stack?.Outputs ?? [];
  if (outputs.length === 0) {
    return;
  }
  console.log("Stack outputs:");
  for (const output of outputs) {
    console.log(`- ${output.OutputKey}: ${output.OutputValue}`);
  }
}

async function deployStack(client: CloudFormationClient, config: DeployConfig): Promise<void> {
  const templatePath = resolve(process.cwd(), "infra", "cloudformation", "shared-network.yml");
  const templateBody = readFileSync(templatePath, "utf-8");
  const parameters = createParameters(config);
  const tags = createTags(config);
  const exists = await stackExists(client, config.stack_name);

  if (exists) {
    try {
      console.log(`Updating stack ${config.stack_name} in ${config.region}...`);
      await client.send(
        new UpdateStackCommand({
          StackName: config.stack_name,
          TemplateBody: templateBody,
          Parameters: parameters,
          Tags: tags
        })
      );
      await waitUntilStackUpdateComplete(
        { client, maxWaitTime: 600 },
        { StackName: config.stack_name }
      );
      console.log(`Stack update complete: ${config.stack_name}`);
    } catch (error) {
      if (isCloudFormationValidationError(error, NO_UPDATES_PATTERN)) {
        console.log(`No changes detected for stack: ${config.stack_name}`);
        return;
      }
      throw error;
    }
  } else {
    console.log(`Creating stack ${config.stack_name} in ${config.region}...`);
    await client.send(
      new CreateStackCommand({
        StackName: config.stack_name,
        TemplateBody: templateBody,
        Parameters: parameters,
        Tags: tags
      })
    );
    await waitUntilStackCreateComplete(
      { client, maxWaitTime: 600 },
      { StackName: config.stack_name }
    );
    console.log(`Stack creation complete: ${config.stack_name}`);
  }

  await printOutputs(client, config.stack_name);
}

async function destroyStack(client: CloudFormationClient, config: DeployConfig): Promise<void> {
  const exists = await stackExists(client, config.stack_name);
  if (!exists) {
    console.log(`Stack does not exist, nothing to destroy: ${config.stack_name}`);
    return;
  }

  console.log(`Deleting stack ${config.stack_name} in ${config.region}...`);
  await client.send(new DeleteStackCommand({ StackName: config.stack_name }));
  await waitUntilStackDeleteComplete(
    { client, maxWaitTime: 600 },
    { StackName: config.stack_name }
  );
  console.log(`Stack deletion complete: ${config.stack_name}`);
}

async function run(): Promise<void> {
  const action = parseActionArg();
  const env = parseEnvArg();
  const config = loadConfig(env);
  const client = new CloudFormationClient({ region: config.region });
  if (action === "destroy") {
    parseConfirmDestroyArg(config.stack_name);
    await destroyStack(client, config);
    return;
  }
  await deployStack(client, config);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
