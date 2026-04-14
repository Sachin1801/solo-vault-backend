# Solo Vault Backend

AWS serverless backend for [Solo IDE](https://github.com/Solo-UDE/solo)'s cloud vault feature.

## Overview

This repository contains the AWS infrastructure and Lambda functions that power Solo IDE's Vault — a cloud-backed knowledge store where users index structured data and files, making them searchable by Solo's AI agent.

## Architecture

```
CloudFront → API Gateway (REST + WebSocket) → Lambda (x8)
                    │
        ┌───────────┼───────────┐
        S3         RDS        DynamoDB
     (files)   (pgvector)   (sessions)
        │
   S3 Event → SQS → Step Functions Pipeline
                     (validate → extract → chunk → embed → store → notify)
                              │
                        EventBridge → SNS → WebSocket (progress)

Supporting: Cognito (auth) | KMS (encryption) | Secrets Manager | CloudWatch
```

## AWS Services (15)

| Service | Purpose |
|---------|---------|
| Cognito | User authentication, JWT tokens |
| API Gateway (REST) | Vault CRUD + search + session sync APIs |
| API Gateway (WebSocket) | Real-time indexing progress |
| Lambda (x8) | Serverless API handlers + pipeline stages |
| S3 | Vault file storage |
| RDS (PostgreSQL + pgvector) | Metadata + vector embeddings |
| SQS | Indexing job queue |
| Step Functions | 6-stage indexing pipeline orchestration |
| EventBridge | Pipeline event routing |
| SNS | Indexing completion notifications |
| DynamoDB | Cloud-synced agent sessions |
| CloudFront | CDN for file delivery |
| CloudWatch | Logging, metrics, alarms |
| KMS | Encryption at rest |
| Secrets Manager | API keys, DB credentials |

## Getting Started

```bash
# Prerequisites
# - AWS CLI configured with appropriate credentials
# - Node.js 20+

# Install dependencies
npm install

# Deploy baseline shared network stack (dev)
npm run deploy

# Deploy staging
npm run deploy:staging
```

Or with Make:

```bash
make install
make deploy STAGE=dev
make deploy STAGE=staging
```

Detailed infra deployment guide: [deploy.md](deploy.md)

## Infrastructure Bootstrap (INFRA-1)

This repository uses **CloudFormation templates deployed via TypeScript AWS SDK v3**.

### Environment Configs

- `infra/config/dev.json`
- `infra/config/staging.json`

Both environments are isolated with:
- separate CloudFormation stack names
- separate VPC CIDR ranges
- environment tags
- configurable `project_prefix` used in resource names/tags

### What `npm run deploy` provisions

The baseline stack provisions:
- shared VPC
- two private subnets
- Lambda security group
- RDS security group that only allows PostgreSQL (5432) from Lambda security group

### Team deployment setup

> **Security note:** Prefer short-lived credentials over long-lived IAM user access keys.
> Long-lived keys are the single most common source of accidentally-exposed AWS
> credentials (committed to git, pasted in chat, leaked via shell history). Use one of
> the options below in order of preference.

#### Option A (recommended): AWS IAM Identity Center (SSO)

Short-lived credentials refreshed via browser login. No permanent access keys on disk.

1. Ask the account admin for an SSO start URL and a permission set scoped to this project.
2. Configure an SSO profile locally:

```bash
aws configure sso
# Follow the prompts, use region: us-east-1
```

3. Sign in and deploy:

```bash
aws sso login --profile solo-vault
AWS_PROFILE=solo-vault npm run deploy
```

#### Option B: AssumeRole with a named profile

Use a low-privilege IAM user (or SSO) to assume a deploy role with CloudFormation
permissions. Keeps the attack surface small.

```ini
# ~/.aws/config
[profile solo-vault-deploy]
role_arn = arn:aws:iam::<account-id>:role/SoloVaultDeploy
source_profile = default
region = us-east-1
```

```bash
AWS_PROFILE=solo-vault-deploy npm run deploy
```

#### Option C (last resort): IAM user access keys

Acceptable for a short-lived course project; **never use for anything production**.

1. Create an IAM user with programmatic access.
2. Attach the minimum permissions required for the baseline stack:
   - `cloudformation:CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`
   - EC2 VPC/subnet/security-group create/update/describe/tag
   - `IAM:PassRole` is **not** required for INFRA-1.
3. Configure credentials locally:

```bash
aws configure
# Use region: us-east-1
```

4. Deploy:

```bash
npm run deploy
npm run deploy:staging
```

**If you use Option C, rotate the keys at least every 90 days, never commit them to
git, and delete the user when the course project ends.**

### How to undo / delete resources

To delete stacks and all resources created by this bootstrap:

```bash
# Delete dev stack
npm run destroy -- --confirm-destroy solo-vault-shared-network-dev

# Delete staging stack
npm run destroy:staging -- --confirm-destroy solo-vault-shared-network-staging
```

Or with Make:

```bash
make destroy STAGE=dev
make destroy STAGE=staging
```

`STAGE` is required for `make deploy` and `make destroy` and must be either `dev` or `staging`.

This uses CloudFormation stack deletion, so resources are removed in dependency-safe order.
Destroy now requires an explicit stack-name confirmation token to reduce accidental deletion risk.

## API Reference

See [docs/API.md](docs/API.md) for the full REST + WebSocket API specification.

## Team

| Role | Scope |
|------|-------|
| **Engineer 1** | AWS infrastructure & IaC (Cognito, API Gateway, S3, RDS, KMS, Secrets Manager) |
| **Engineer 2** | Lambda functions & API development (CRUD, files, auth, WebSocket) |
| **Engineer 3** | Pipeline & data (Step Functions, SQS, EventBridge, SNS, DynamoDB, search) |

## Related

- [Solo IDE](https://github.com/Solo-UDE/solo) — The desktop application that consumes this backend
- [Design Spec](docs/DESIGN_SPEC.md) — Full technical specification
- [Work Breakdown](docs/WORK_BREAKDOWN.md) — Detailed tickets and assignments
