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
# - Node.js 20+ (Lambda runtime)
# - AWS CDK or SAM CLI for deployment

# Install dependencies
npm install

# Deploy to AWS
npm run deploy

# Run locally
npm run dev
```

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
