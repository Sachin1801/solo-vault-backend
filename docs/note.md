# Handoff Note — Pipeline (Engineer 3 → Team)

**Branch:** `services/indexer`
**PR:** https://github.com/Sachin1801/solo-vault-backend/pull/new/services/indexer
**Dataset zip:** `~/Desktop/solo-vault-test-dataset.zip` (104 MB, 808 files)

I have my Amazon SDE interview in-person in Seattle on Friday so I need to fly out.
Everything is coded, CloudFormation'd, and deploy-scripted. What remains is
running the deploy and testing. Here's everything you need.

---

## What I built

The indexing pipeline is fully coded and split into 5 Lambda/ECS stages:

```
S3 upload -> SQS -> Step Functions:
  1. fn-validate        (Lambda)           -- checks MIME, size, S3 existence
  2. fn-download-parse  (Lambda container) -- downloads + parses by file type
  3. fn-chunk           (Lambda)           -- tokenizes into 500-token chunks
  4. fn-embed           (ECS Fargate)      -- BGE-M3 embedding (1.5GB model)
  5. fn-store           (Lambda VPC)       -- writes to RDS pgvector

Step Functions state change -> EventBridge -> SNS -> ws-notify
```

---

## Where to look

| What | Where |
|------|-------|
| **Start here** | `docs/decision.md` -- full architecture decision with trade-offs |
| Architecture diagrams | `docs/architecture-option-b-recommended.drawio` (open at diagrams.net) |
| Migration plan | `services/indexer/plan.md` |
| Current state + what's left | `services/indexer/state.md` |
| Lambda handler code | `services/indexer/lambdas/fn_*/handler.py` |
| ECS embed entrypoint | `services/indexer/lambdas/fn_embed/entrypoint.py` |
| Dockerfiles | `services/indexer/lambdas/fn_embed/Dockerfile`, `fn_download_parse/Dockerfile` |
| CloudFormation stacks | `infra/cloudformation/sqs-pipeline.yml`, `pipeline-lambdas.yml`, `step-functions-pipeline.yml`, `notifications-pipeline.yml` |
| Step Functions ASL | `infra/step-functions/pipeline.asl.json` |
| **One-command deploy script** | `infra/scripts/deploy-all-pipeline.sh` |
| Dataset upload script | `services/indexer/scripts/upload_dataset.py` |

---

## How to deploy (one command)

```bash
# Prerequisites: AWS CLI configured, Docker running, npm install done
make deploy-pipeline STAGE=dev
```

This script (`infra/scripts/deploy-all-pipeline.sh`) does everything automatically:

1. Deploys SQS stack
2. Reads your VPC/subnet/SG outputs from `shared-network` stack
3. Deploys Lambda functions + IAM roles + ECR repos + ECS cluster
4. Builds Docker images and pushes to ECR
5. Uploads Lambda code (zip + `aws lambda update-function-code`)
6. Deploys Step Functions with real ARNs
7. Deploys EventBridge + SNS notifications

---

## Dataset

I'm sharing the test dataset as a zip file: **`solo-vault-test-dataset.zip`** (104 MB, 808 files).

After deploying, upload it:

```bash
# Unzip to a local directory, then:
python services/indexer/scripts/upload_dataset.py \
  --bucket solo-vault-vault-dev --no-endpoint

# Or trigger indexing too:
python services/indexer/scripts/upload_dataset.py \
  --bucket solo-vault-vault-dev --no-endpoint \
  --trigger --sfn-arn <state-machine-arn-from-deploy-output>
```

---

## Critical: Vector dimension mismatch

Michael's RDS PR (`feat/infra-5-rds-pgvector`) uses `vector(1536)`. My indexer
uses BGE-M3 which outputs `vector(1024)`. **Before merging his PR, change `1536`
to `1024`** in his `db/schema.sql`. Otherwise vectors won't match and search
will return garbage.

---

## What might need tweaking

1. **The deploy script assumes shared-network stack outputs exist** -- if stack
   names differ, update the `NETWORK_STACK` variable in `deploy-all-pipeline.sh`
2. **fn-download-parse Dockerfile** needs the `app/` package copied during build
   -- the deploy script handles this (`cp -r services/indexer/app ./app/`) but
   if you build manually, you need to do this step
3. **fn-embed Docker build takes ~10 min** first time (downloads BGE-M3 model
   ~1.5GB) -- subsequent builds are cached
4. **fn-store needs DB credentials** -- set `DB_HOST`, `DB_USER`, `DB_PASSWORD`
   as Lambda env vars (or pull from Secrets Manager via `DB_SECRET_ARN`)

---

Thanks for picking this up. The hard part (architecture decisions, code
extraction, IaC) is done. What remains is running the deploy and testing.
Good luck with the demo!
