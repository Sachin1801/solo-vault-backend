#!/usr/bin/env node
/**
 * Deploy the indexing pipeline CloudFormation stacks in dependency order.
 *
 * Usage:
 *   npx tsx infra/scripts/deploy-pipeline.ts --env dev
 *   npx tsx infra/scripts/deploy-pipeline.ts --env dev --action destroy --confirm-destroy solo-vault
 *   npx tsx infra/scripts/deploy-pipeline.ts --env dev --stack sqs   # deploy just one stack
 *
 * Deploy order (dependencies flow top-down):
 *   1. sqs-pipeline          (no dependencies)
 *   2. notifications-pipeline (depends on SFN ARN — uses placeholder, updated after SFN deploy)
 *   3. step-functions-pipeline (depends on Lambda ARNs — uses placeholders until Lambdas exist)
 *
 * NOTE: Lambda functions and ECS task definitions must be deployed separately
 * (via SAM, CDK, or manual CLI) before the Step Functions stack can invoke them.
 * The CloudFormation stacks accept Lambda/ECS ARNs as parameters so you can
 * deploy the infra skeleton first, then wire in real ARNs after Lambda deploy.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  CloudFormationClient,
  CreateStackCommand,
  DeleteStackCommand,
  DescribeStacksCommand,
  UpdateStackCommand,
  waitUntilStackCreateComplete,
  waitUntilStackDeleteComplete,
  waitUntilStackUpdateComplete,
  type Parameter,
  type Tag,
} from "@aws-sdk/client-cloudformation";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Environment = "dev" | "staging";
type Action = "deploy" | "destroy";

interface StackDef {
  name: string;
  templateFile: string;
  parameters: Record<string, string>;
  capabilities?: string[];
}

// ---------------------------------------------------------------------------
// CLI parsing
// ---------------------------------------------------------------------------

function parseArg(flag: string, fallback?: string): string {
  const idx = process.argv.findIndex((a) => a === `--${flag}`);
  if (idx === -1 || idx + 1 >= process.argv.length) {
    if (fallback !== undefined) return fallback;
    throw new Error(`Missing --${flag} argument`);
  }
  return process.argv[idx + 1];
}

function hasFlag(flag: string): boolean {
  return process.argv.includes(`--${flag}`);
}

// ---------------------------------------------------------------------------
// Stack definitions for the pipeline
// ---------------------------------------------------------------------------

function getPipelineStacks(env: Environment, projectPrefix: string): StackDef[] {
  const placeholder = "arn:aws:lambda:us-east-1:000000000000:function:placeholder";
  const ecsPlaceholder = "arn:aws:ecs:us-east-1:000000000000:cluster/placeholder";
  const taskPlaceholder = "arn:aws:ecs:us-east-1:000000000000:task-definition/placeholder:1";

  return [
    // 1. SQS — no dependencies
    {
      name: `${projectPrefix}-sqs-pipeline-${env}`,
      templateFile: "sqs-pipeline.yml",
      parameters: {
        ProjectPrefix: projectPrefix,
        Environment: env,
      },
    },

    // 2. Step Functions — depends on Lambda ARNs (placeholders until deployed)
    {
      name: `${projectPrefix}-sfn-pipeline-${env}`,
      templateFile: "step-functions-pipeline.yml",
      parameters: {
        ProjectPrefix: projectPrefix,
        Environment: env,
        FnValidateArn: placeholder,
        FnDownloadParseArn: placeholder,
        FnChunkArn: placeholder,
        FnStoreArn: placeholder,
        EcsClusterArn: ecsPlaceholder,
        EmbedTaskDefArn: taskPlaceholder,
        PrivateSubnets: "subnet-placeholder",
        LambdaSecurityGroup: "sg-placeholder",
      },
      capabilities: ["CAPABILITY_NAMED_IAM"],
    },

    // 3. Notifications — depends on SFN ARN (placeholder until SFN deployed)
    {
      name: `${projectPrefix}-notifications-pipeline-${env}`,
      templateFile: "notifications-pipeline.yml",
      parameters: {
        ProjectPrefix: projectPrefix,
        Environment: env,
        StateMachineArn: `arn:aws:states:us-east-1:000000000000:stateMachine:${projectPrefix}-index-pipeline-${env}`,
      },
    },
  ];
}

// ---------------------------------------------------------------------------
// CloudFormation helpers (same pattern as Sachin's deploy.ts)
// ---------------------------------------------------------------------------

function isValidationError(error: unknown, pattern: RegExp): boolean {
  if (!(error instanceof Error)) return false;
  if (error.name !== "ValidationError") return false;
  return pattern.test(error.message);
}

async function stackExists(client: CloudFormationClient, name: string): Promise<boolean> {
  try {
    await client.send(new DescribeStacksCommand({ StackName: name }));
    return true;
  } catch (error) {
    if (isValidationError(error, /does not exist/i)) return false;
    throw error;
  }
}

async function deployStack(
  client: CloudFormationClient,
  stackDef: StackDef,
  tags: Tag[]
): Promise<void> {
  const templatePath = resolve(process.cwd(), "infra", "cloudformation", stackDef.templateFile);
  const templateBody = readFileSync(templatePath, "utf-8");
  const parameters: Parameter[] = Object.entries(stackDef.parameters).map(([k, v]) => ({
    ParameterKey: k,
    ParameterValue: v,
  }));

  const exists = await stackExists(client, stackDef.name);
  const capabilities = (stackDef.capabilities ?? []) as any;

  if (exists) {
    try {
      console.log(`  Updating ${stackDef.name}...`);
      await client.send(
        new UpdateStackCommand({
          StackName: stackDef.name,
          TemplateBody: templateBody,
          Parameters: parameters,
          Tags: tags,
          Capabilities: capabilities,
        })
      );
      await waitUntilStackUpdateComplete(
        { client, maxWaitTime: 600 },
        { StackName: stackDef.name }
      );
      console.log(`  ✓ Updated: ${stackDef.name}`);
    } catch (error) {
      if (isValidationError(error, /no updates are to be performed/i)) {
        console.log(`  - No changes: ${stackDef.name}`);
        return;
      }
      throw error;
    }
  } else {
    console.log(`  Creating ${stackDef.name}...`);
    await client.send(
      new CreateStackCommand({
        StackName: stackDef.name,
        TemplateBody: templateBody,
        Parameters: parameters,
        Tags: tags,
        Capabilities: capabilities,
      })
    );
    await waitUntilStackCreateComplete(
      { client, maxWaitTime: 600 },
      { StackName: stackDef.name }
    );
    console.log(`  ✓ Created: ${stackDef.name}`);
  }

  // Print outputs
  const result = await client.send(new DescribeStacksCommand({ StackName: stackDef.name }));
  const outputs = result.Stacks?.[0]?.Outputs ?? [];
  for (const o of outputs) {
    console.log(`    ${o.OutputKey}: ${o.OutputValue}`);
  }
}

async function destroyStack(client: CloudFormationClient, stackName: string): Promise<void> {
  const exists = await stackExists(client, stackName);
  if (!exists) {
    console.log(`  - Does not exist: ${stackName}`);
    return;
  }
  console.log(`  Deleting ${stackName}...`);
  await client.send(new DeleteStackCommand({ StackName: stackName }));
  await waitUntilStackDeleteComplete(
    { client, maxWaitTime: 600 },
    { StackName: stackName }
  );
  console.log(`  ✓ Deleted: ${stackName}`);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function run(): Promise<void> {
  const env = parseArg("env") as Environment;
  if (env !== "dev" && env !== "staging") {
    throw new Error("--env must be dev or staging");
  }

  const action: Action = (parseArg("action", "deploy") as Action);
  const onlyStack = hasFlag("stack") ? parseArg("stack") : null;

  // Load config
  const configPath = resolve(process.cwd(), "infra", "config", `${env}.json`);
  const config = JSON.parse(readFileSync(configPath, "utf-8"));
  const projectPrefix: string = config.project_prefix;
  const region: string = config.region;

  const client = new CloudFormationClient({ region });
  const tags: Tag[] = Object.entries(config.tags ?? {}).map(([Key, Value]) => ({
    Key,
    Value: Value as string,
  }));

  let stacks = getPipelineStacks(env, projectPrefix);

  // Filter to single stack if --stack is specified
  if (onlyStack) {
    stacks = stacks.filter((s) => s.templateFile.includes(onlyStack));
    if (stacks.length === 0) {
      throw new Error(`No stack matching "${onlyStack}". Available: sqs, sfn, notifications`);
    }
  }

  if (action === "destroy") {
    const confirmToken = parseArg("confirm-destroy");
    if (confirmToken !== projectPrefix) {
      throw new Error(`Destroy confirmation mismatch. Expected: --confirm-destroy ${projectPrefix}`);
    }
    console.log(`\nDestroying pipeline stacks (${env})...\n`);
    // Destroy in reverse order
    for (const stack of [...stacks].reverse()) {
      await destroyStack(client, stack.name);
    }
  } else {
    console.log(`\nDeploying pipeline stacks (${env})...\n`);
    for (const stack of stacks) {
      await deployStack(client, stack, tags);
      console.log("");
    }
  }

  console.log("Done.\n");

  if (action === "deploy") {
    console.log("NEXT STEPS:");
    console.log("  1. Deploy Lambda functions (SAM/CDK or AWS CLI zip upload)");
    console.log("  2. Deploy ECS task definition for fn-embed");
    console.log("  3. Update SFN stack with real Lambda/ECS ARNs:");
    console.log(`     npx tsx infra/scripts/deploy-pipeline.ts --env ${env} --stack sfn`);
    console.log("  4. Upload dataset:");
    console.log("     python services/indexer/scripts/upload_dataset.py --bucket solo-vault-dev");
    console.log("");
  }
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
