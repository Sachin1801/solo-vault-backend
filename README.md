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

### Team deployment setup (IAM users)

All team members can deploy from local machines with direct IAM user credentials.

1. Create/access an IAM user with programmatic access.
2. Attach permissions required for this ticket's baseline stack:
   - CloudFormation stack create/update/describe
   - EC2 VPC/subnet/security-group create/update/describe/tag
   - IAM `PassRole` is not required for INFRA-1 baseline
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
