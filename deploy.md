# Solo Vault Infra Deploy Guide

This guide covers what the current infrastructure bootstrap deploys and how to deploy or destroy it safely.

## Scope of current deployment

Current stack provisions the **shared network baseline** only:

- 1 VPC
- 2 private subnets (AZ-a, AZ-b)
- 1 Lambda security group
- 1 RDS security group
- RDS SG inbound rule: PostgreSQL `5432` allowed only from Lambda SG

No API Gateway, Lambda functions, RDS instance, Cognito, or pipeline services are deployed in this step.

## Environments

- `dev` stack: `solo-vault-shared-network-dev`
- `staging` stack: `solo-vault-shared-network-staging`
- Region: `us-east-1`

Environment config files:

- `infra/config/dev.json`
- `infra/config/staging.json`

## Prerequisites

1. AWS credentials configured locally (`aws configure`)
2. Node.js 20+

Install dependencies:

```bash
npm install
```

or

```bash
make install
```

## Deploy commands

### npm

```bash
npm run deploy
npm run deploy:staging
```

### Make (explicit stage required)

```bash
make deploy STAGE=dev
make deploy STAGE=staging
```

## Destroy commands (undo)

### npm

```bash
npm run destroy -- --confirm-destroy solo-vault-shared-network-dev
npm run destroy:staging -- --confirm-destroy solo-vault-shared-network-staging
```

### Make (explicit stage required)

```bash
make destroy STAGE=dev
make destroy STAGE=staging
```

Destroy uses CloudFormation deletion, so resources are removed in dependency-safe order.
