# Solo Vault Infra Deploy Guide

This guide covers what the current infrastructure bootstrap deploys and how to deploy or destroy it safely.

## Stacks

The deploy script supports multiple CloudFormation stacks via the `--stack` flag.
Each stack has a YAML template under `infra/cloudformation/` and a config entry
under `stacks.<name>` in `infra/config/{env}.json`.

| Stack            | Template                                      | What it creates                                                                                                  |
|------------------|-----------------------------------------------|------------------------------------------------------------------------------------------------------------------|
| `shared-network` | `infra/cloudformation/shared-network.yml`     | 1 VPC, 2 private subnets (AZ-a, AZ-b), Lambda SG, RDS SG (5432 inbound from Lambda SG only)                      |
| `secrets`        | `infra/cloudformation/secrets.yml`            | 2 customer-managed KMS keys (S3, RDS) + aliases; 3 Secrets Manager secrets (db-credentials, embedding-api-key, cloudfront-key-pair) |
| `rds`            | `infra/cloudformation/rds.yml`                | Postgres 15 instance (pgvector-capable), DB subnet group, SecretTargetAttachment filling db-credentials with host/port/dbname |

### Deployment order

`shared-network` and `secrets` are independent — deploy in any order.
`rds` depends on exports from both (subnet IDs, RDS SG, RDS KMS key, db-credentials secret), so it must be deployed after both.

```
shared-network  ┐
                ├──►  rds
secrets         ┘
```

## Environments

- `dev`:
  - `solo-vault-shared-network-dev`
  - `solo-vault-secrets-dev`
- `staging`:
  - `solo-vault-shared-network-staging`
  - `solo-vault-secrets-staging`
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

### Make (recommended)

```bash
make deploy STAGE=dev STACK=shared-network
make deploy STAGE=dev STACK=secrets
make deploy STAGE=dev STACK=rds

make deploy STAGE=staging STACK=shared-network
make deploy STAGE=staging STACK=secrets
make deploy STAGE=staging STACK=rds
```

Note: `rds` typically takes 10–15 minutes for the initial create.

`STACK` defaults to `shared-network` if omitted (backwards-compatible with the
original single-stack workflow).

### npm shortcuts

The named `deploy` scripts are pinned to `shared-network` for backwards compatibility.
For other stacks, call `iac` directly:

```bash
# shared-network (dev/staging shortcuts)
npm run deploy
npm run deploy:staging

# any stack
npm run iac -- --env dev     --stack secrets
npm run iac -- --env staging --stack secrets
```

## Destroy commands (undo)

### Make

```bash
make destroy STAGE=dev STACK=secrets
make destroy STAGE=dev STACK=shared-network
```

### npm

```bash
# shared-network (dev/staging shortcuts)
npm run destroy  -- --confirm-destroy solo-vault-shared-network-dev
npm run destroy:staging -- --confirm-destroy solo-vault-shared-network-staging

# any stack
npm run iac -- --action destroy --env dev --stack secrets \
  --confirm-destroy solo-vault-secrets-dev
```

Destroy uses CloudFormation deletion, so resources are removed in dependency-safe order.

## Post-deploy: populating placeholder secrets

The `secrets` stack ships two secrets with `REPLACE_ME` placeholder values
because CloudFormation can't hold real secret material safely. After the stack
deploys, populate them with `aws secretsmanager put-secret-value`:

### Embedding API key

```bash
aws secretsmanager put-secret-value \
  --secret-id solo-vault/dev/embedding-api-key \
  --secret-string '{"api_key": "sk-..."}'
```

### CloudFront signed-URL key pair

Generate a CloudFront key pair in the AWS console (or via the CLI), then:

```bash
aws secretsmanager put-secret-value \
  --secret-id solo-vault/dev/cloudfront-key-pair \
  --secret-string "$(jq -n \
    --arg kid 'K2EXAMPLE' \
    --arg pem "$(cat private_key.pem)" \
    '{key_pair_id: $kid, private_key: $pem}')"
```

### DB credentials

The `db-credentials` secret is auto-populated at secrets-stack-create time with a
random 32-character password and the configured `DbUsername`. Host, port, engine,
and dbname fields are filled in by `AWS::SecretsManager::SecretTargetAttachment`
inside the `rds` stack — no manual step needed.

## Running the database schema migration

After the `rds` stack is `CREATE_COMPLETE`, apply the schema:

```bash
npm run migrate:dev
# or
npm run migrate:staging
```

To apply a different SQL file (e.g. a future migration):

```bash
npm run migrate -- --env dev --file db/migrations/001_example.sql
```

### What the migration runner does

RDS lives in private subnets, so you can't `psql` from your laptop. The
`migrate` script spins up a one-shot Lambda inside the Lambda SG to do the
work from inside the VPC:

1. Resolves network + secret ARN from CloudFormation exports
2. Bundles `infra/lambda/db-migrate/handler.ts` with `esbuild`, zips it
3. Creates a short-lived IAM role (VPC access + read on `db-credentials`)
4. Deploys the Lambda into both private subnets + Lambda SG
5. Invokes synchronously with the SQL payload, prints back the created
   extensions and tables
6. Deletes the Lambda + role — nothing persistent left behind

The schema uses `IF NOT EXISTS` throughout, so re-running is safe.
