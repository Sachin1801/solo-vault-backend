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

type StackName = "shared-network" | "secrets" | "rds";

const STACK_NAMES: readonly StackName[] = ["shared-network", "secrets", "rds"];

type StackConfig = {
  stack_name: string;
  parameters: Record<string, string>;
};

type DeployConfig = {
  project_prefix: string;
  environment: Environment;
  region: string;
  tags?: Record<string, string>;
  stacks: Record<StackName, StackConfig>;
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

function parseStackArg(): StackName {
  const stackIndex = process.argv.findIndex((arg) => arg === "--stack");
  if (stackIndex === -1 || stackIndex + 1 >= process.argv.length) {
    throw new Error(
      `Missing --stack argument. Allowed: ${STACK_NAMES.join(", ")}.`
    );
  }
  const stackValue = process.argv[stackIndex + 1];
  if (!STACK_NAMES.includes(stackValue as StackName)) {
    throw new Error(
      `Invalid --stack value "${stackValue}". Allowed: ${STACK_NAMES.join(", ")}.`
    );
  }
  return stackValue as StackName;
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

function resolveStackConfig(config: DeployConfig, stack: StackName): StackConfig {
  const stackConfig = config.stacks?.[stack];
  if (!stackConfig) {
    throw new Error(
      `Config is missing an entry for stack "${stack}". Add it under the "stacks" key in the env config.`
    );
  }
  return stackConfig;
}

// Every template takes ProjectPrefix + EnvironmentName; stack-specific parameters
// are merged in from the env config so this script doesn't need to know which
// parameters belong to which template.
function createParameters(config: DeployConfig, stackConfig: StackConfig): Parameter[] {
  const base: Parameter[] = [
    { ParameterKey: "ProjectPrefix", ParameterValue: config.project_prefix },
    { ParameterKey: "EnvironmentName", ParameterValue: config.environment }
  ];
  const custom = Object.entries(stackConfig.parameters ?? {}).map(
    ([ParameterKey, ParameterValue]): Parameter => ({ ParameterKey, ParameterValue })
  );
  return [...base, ...custom];
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

async function deployStack(
  client: CloudFormationClient,
  config: DeployConfig,
  stack: StackName,
  stackConfig: StackConfig
): Promise<void> {
  const templatePath = resolve(process.cwd(), "infra", "cloudformation", `${stack}.yml`);
  const templateBody = readFileSync(templatePath, "utf-8");
  const parameters = createParameters(config, stackConfig);
  const tags = createTags(config);
  const exists = await stackExists(client, stackConfig.stack_name);

  if (exists) {
    try {
      console.log(`Updating stack ${stackConfig.stack_name} in ${config.region}...`);
      await client.send(
        new UpdateStackCommand({
          StackName: stackConfig.stack_name,
          TemplateBody: templateBody,
          Parameters: parameters,
          Tags: tags
        })
      );
      await waitUntilStackUpdateComplete(
        { client, maxWaitTime: 600 },
        { StackName: stackConfig.stack_name }
      );
      console.log(`Stack update complete: ${stackConfig.stack_name}`);
    } catch (error) {
      if (isCloudFormationValidationError(error, NO_UPDATES_PATTERN)) {
        console.log(`No changes detected for stack: ${stackConfig.stack_name}`);
        return;
      }
      throw error;
    }
  } else {
    console.log(`Creating stack ${stackConfig.stack_name} in ${config.region}...`);
    await client.send(
      new CreateStackCommand({
        StackName: stackConfig.stack_name,
        TemplateBody: templateBody,
        Parameters: parameters,
        Tags: tags
      })
    );
    await waitUntilStackCreateComplete(
      { client, maxWaitTime: 600 },
      { StackName: stackConfig.stack_name }
    );
    console.log(`Stack creation complete: ${stackConfig.stack_name}`);
  }

  await printOutputs(client, stackConfig.stack_name);
}

async function destroyStack(
  client: CloudFormationClient,
  stackConfig: StackConfig,
  region: string
): Promise<void> {
  const exists = await stackExists(client, stackConfig.stack_name);
  if (!exists) {
    console.log(`Stack does not exist, nothing to destroy: ${stackConfig.stack_name}`);
    return;
  }

  console.log(`Deleting stack ${stackConfig.stack_name} in ${region}...`);
  await client.send(new DeleteStackCommand({ StackName: stackConfig.stack_name }));
  await waitUntilStackDeleteComplete(
    { client, maxWaitTime: 600 },
    { StackName: stackConfig.stack_name }
  );
  console.log(`Stack deletion complete: ${stackConfig.stack_name}`);
}

async function run(): Promise<void> {
  const action = parseActionArg();
  const env = parseEnvArg();
  const stack = parseStackArg();
  const config = loadConfig(env);
  const stackConfig = resolveStackConfig(config, stack);
  const client = new CloudFormationClient({ region: config.region });
  if (action === "destroy") {
    parseConfirmDestroyArg(stackConfig.stack_name);
    await destroyStack(client, stackConfig, config.region);
    return;
  }
  await deployStack(client, config, stack, stackConfig);
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
