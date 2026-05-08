#!/usr/bin/env bash
# ============================================================================
# deploy-all-pipeline.sh — One-command pipeline deployment
#
# Usage:
#   ./infra/scripts/deploy-all-pipeline.sh dev
#   ./infra/scripts/deploy-all-pipeline.sh staging
#
# What it does (in order):
#   1. Deploy SQS stack (queue + DLQ)
#   2. Deploy pipeline-lambdas stack (IAM, ECR, Lambda functions, ECS cluster)
#   3. Build + push Docker images to ECR (fn-download-parse, fn-embed)
#   4. Update Lambda function code (fn-validate, fn-chunk, fn-store)
#   5. Deploy Step Functions stack (wired to real Lambda/ECS ARNs)
#   6. Re-point SQS starter at the real Step Functions ARN
#   7. Deploy notifications stack (EventBridge + SNS)
#   8. Print summary
#
# Prerequisites:
#   - AWS CLI configured (aws sts get-caller-identity works)
#   - Docker running
#   - Node.js 20+ with npm dependencies installed
# ============================================================================

set -euo pipefail

ENV="${1:-dev}"
PROJECT="solo-vault"
REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROOT_DIR=$(cd "$(dirname "$0")/../.." && pwd)
INDEXER_DIR="${ROOT_DIR}/services/indexer"
CFN_DIR="${ROOT_DIR}/infra/cloudformation"
IMAGE_TAG=$(date -u +%Y%m%d%H%M%S)
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"

cleanup() {
    rm -rf "${INDEXER_DIR}/lambdas/fn_download_parse/app"
}
trap cleanup EXIT

echo "============================================"
echo "  Solo Vault Pipeline Deploy"
echo "  Environment: ${ENV}"
echo "  Account:     ${ACCOUNT_ID}"
echo "  Region:      ${REGION}"
echo "  Docker arch: ${DOCKER_PLATFORM}"
echo "============================================"
echo ""

# Helper: deploy a CloudFormation stack
deploy_stack() {
    local STACK_NAME="$1"
    local TEMPLATE="$2"
    shift 2
    local PARAMS=("$@")

    echo ">> Deploying stack: ${STACK_NAME}"

    local PARAM_OVERRIDES=""
    for p in "${PARAMS[@]}"; do
        PARAM_OVERRIDES="${PARAM_OVERRIDES} ParameterKey=${p%%=*},ParameterValue=${p#*=}"
    done

    # Check if stack exists
    if aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" >/dev/null 2>&1; then
        echo "   Updating existing stack..."
        local UPDATE_OUTPUT
        if ! UPDATE_OUTPUT=$(aws cloudformation update-stack \
            --stack-name "${STACK_NAME}" \
            --template-body "file://${CFN_DIR}/${TEMPLATE}" \
            --parameters ${PARAM_OVERRIDES} \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "${REGION}" 2>&1); then
            if grep -q "No updates are to be performed" <<< "${UPDATE_OUTPUT}"; then
                echo "   No changes."
                return 0
            fi
            echo "${UPDATE_OUTPUT}" >&2
            return 1
        fi
        aws cloudformation wait stack-update-complete \
            --stack-name "${STACK_NAME}" --region "${REGION}" 2>/dev/null || true
    else
        echo "   Creating new stack..."
        aws cloudformation create-stack \
            --stack-name "${STACK_NAME}" \
            --template-body "file://${CFN_DIR}/${TEMPLATE}" \
            --parameters ${PARAM_OVERRIDES} \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "${REGION}"
        aws cloudformation wait stack-create-complete \
            --stack-name "${STACK_NAME}" --region "${REGION}"
    fi
    echo "   Done: ${STACK_NAME}"
    echo ""
}

# Helper: get CloudFormation output value
get_output() {
    local STACK_NAME="$1"
    local OUTPUT_KEY="$2"
    aws cloudformation describe-stacks \
        --stack-name "${STACK_NAME}" \
        --region "${REGION}" \
        --query "Stacks[0].Outputs[?OutputKey=='${OUTPUT_KEY}'].OutputValue" \
        --output text
}

get_export() {
    local EXPORT_NAME="$1"
    aws cloudformation list-exports \
        --region "${REGION}" \
        --query "Exports[?Name=='${EXPORT_NAME}'].Value" \
        --output text \
        | tr '\t' '\n' \
        | awk 'NF && $0 != "None" { print; exit }'
}

require_value() {
    local NAME="$1"
    local VALUE="$2"
    if [[ -z "${VALUE}" || "${VALUE}" == "None" ]]; then
        echo "Missing required value: ${NAME}" >&2
        exit 1
    fi
}

ensure_ecr_repo() {
    local REPO_NAME="$1"
    if aws ecr describe-repositories --repository-names "${REPO_NAME}" --region "${REGION}" >/dev/null 2>&1; then
        return 0
    fi
    aws ecr create-repository \
        --repository-name "${REPO_NAME}" \
        --image-scanning-configuration scanOnPush=true \
        --region "${REGION}" \
        --no-cli-pager >/dev/null
}

install_lambda_deps() {
    local TARGET_DIR="$1"
    shift
    docker run --rm \
        --platform "${DOCKER_PLATFORM}" \
        --entrypoint /bin/sh \
        -v "${TARGET_DIR}:/asset-output" \
        public.ecr.aws/lambda/python:3.12 \
        -c "python -m pip install --quiet --target /asset-output $*"
}

# ============================================================================
# Step 1: Deploy SQS
# ============================================================================
echo "=== Step 1/6: SQS Queue ==="
deploy_stack "${PROJECT}-sqs-pipeline-${ENV}" "sqs-pipeline.yml" \
    "ProjectPrefix=${PROJECT}" \
    "Environment=${ENV}"

SQS_ARN=$(get_output "${PROJECT}-sqs-pipeline-${ENV}" "IndexQueueArn")
SQS_URL=$(get_output "${PROJECT}-sqs-pipeline-${ENV}" "IndexQueueUrl")
EMBED_QUEUE_ARN=$(get_output "${PROJECT}-sqs-pipeline-${ENV}" "EmbedQueueArn")
EMBED_QUEUE_URL=$(get_output "${PROJECT}-sqs-pipeline-${ENV}" "EmbedQueueUrl")
EMBED_QUEUE_NAME=$(get_output "${PROJECT}-sqs-pipeline-${ENV}" "EmbedQueueName")
EMBED_DESIRED_COUNT="${EMBED_DESIRED_COUNT:-1}"
EMBED_MAX_CAPACITY="${EMBED_MAX_CAPACITY:-2}"
echo "   SQS ARN: ${SQS_ARN}"
echo "   Embed queue: ${EMBED_QUEUE_URL}"
echo "   Embed workers: desired=${EMBED_DESIRED_COUNT}, max=${EMBED_MAX_CAPACITY}"

# ============================================================================
# Step 2: Get network outputs from shared-network stack
# ============================================================================
echo "=== Step 2/6: Reading network config ==="
NETWORK_STACK="${PROJECT}-shared-network-${ENV}"
VPC_ID=$(get_output "${NETWORK_STACK}" "VpcId" 2>/dev/null || echo "vpc-placeholder")
SUBNET_A=$(get_output "${NETWORK_STACK}" "PrivateSubnetAId" 2>/dev/null || echo "subnet-placeholder-a")
SUBNET_B=$(get_output "${NETWORK_STACK}" "PrivateSubnetBId" 2>/dev/null || echo "subnet-placeholder-b")
LAMBDA_SG=$(get_output "${NETWORK_STACK}" "LambdaSecurityGroupId" 2>/dev/null || echo "sg-placeholder")
echo "   VPC: ${VPC_ID}, Subnets: ${SUBNET_A}, ${SUBNET_B}, SG: ${LAMBDA_SG}"
echo ""

echo "=== Step 2b/6: Reading storage + DB config ==="
VAULT_FILES_BUCKET=$(get_export "${PROJECT}-${ENV}-vault-files-bucket")
DB_SECRET_ARN=$(get_export "${PROJECT}-${ENV}-db-credentials-arn")
require_value "${PROJECT}-${ENV}-vault-files-bucket" "${VAULT_FILES_BUCKET}"
require_value "${PROJECT}-${ENV}-db-credentials-arn" "${DB_SECRET_ARN}"
echo "   Vault files bucket: ${VAULT_FILES_BUCKET}"
echo "   DB secret:          ${DB_SECRET_ARN}"
echo ""

echo "=== Step 2c/6: Build and push container images ==="
ECR_DP_REPO="${PROJECT}-fn-download-parse-${ENV}"
ECR_EMBED_REPO="${PROJECT}-fn-embed-${ENV}"
ECR_DP_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_DP_REPO}"
ECR_EMBED_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_EMBED_REPO}"
ensure_ecr_repo "${ECR_DP_REPO}"
ensure_ecr_repo "${ECR_EMBED_REPO}"
aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

echo "   Building fn-download-parse image..."
cd "${INDEXER_DIR}/lambdas/fn_download_parse"
rm -rf ./app/
cp -r "${INDEXER_DIR}/app" ./app/
docker build --platform "${DOCKER_PLATFORM}" --provenance=false -t "${ECR_DP_REPO}:${IMAGE_TAG}" .
docker tag "${ECR_DP_REPO}:${IMAGE_TAG}" "${ECR_DP_URI}:${IMAGE_TAG}"
docker tag "${ECR_DP_REPO}:${IMAGE_TAG}" "${ECR_DP_URI}:latest"
docker push "${ECR_DP_URI}:${IMAGE_TAG}"
docker push "${ECR_DP_URI}:latest"
rm -rf ./app/

echo "   Building fn-embed image..."
cd "${INDEXER_DIR}/lambdas/fn_embed"
docker build --platform "${DOCKER_PLATFORM}" --provenance=false -t "${ECR_EMBED_REPO}:${IMAGE_TAG}" .
docker tag "${ECR_EMBED_REPO}:${IMAGE_TAG}" "${ECR_EMBED_URI}:${IMAGE_TAG}"
docker tag "${ECR_EMBED_REPO}:${IMAGE_TAG}" "${ECR_EMBED_URI}:latest"
docker push "${ECR_EMBED_URI}:${IMAGE_TAG}"
docker push "${ECR_EMBED_URI}:latest"
echo "   Image tag: ${IMAGE_TAG}"
echo ""

# ============================================================================
# Step 3: Deploy Lambda + ECS + IAM stack (creates functions with placeholder code)
# ============================================================================
echo "=== Step 3/6: Lambda Functions + ECS + IAM ==="

# We use the SFN ARN placeholder here — will be updated after SFN deploys
SFN_ARN_PLACEHOLDER="arn:aws:states:${REGION}:${ACCOUNT_ID}:stateMachine:${PROJECT}-index-pipeline-${ENV}"

deploy_stack "${PROJECT}-pipeline-lambdas-${ENV}" "pipeline-lambdas.yml" \
    "ProjectPrefix=${PROJECT}" \
    "Environment=${ENV}" \
    "VpcId=${VPC_ID}" \
    "PrivateSubnetA=${SUBNET_A}" \
    "PrivateSubnetB=${SUBNET_B}" \
    "LambdaSecurityGroup=${LAMBDA_SG}" \
    "IndexQueueArn=${SQS_ARN}" \
    "IndexQueueUrl=${SQS_URL}" \
    "EmbedQueueArn=${EMBED_QUEUE_ARN}" \
    "EmbedQueueUrl=${EMBED_QUEUE_URL}" \
    "EmbedQueueName=${EMBED_QUEUE_NAME}" \
    "EmbedDesiredCount=${EMBED_DESIRED_COUNT}" \
    "EmbedMaxCapacity=${EMBED_MAX_CAPACITY}" \
    "StateMachineArn=${SFN_ARN_PLACEHOLDER}" \
    "DbSecretArn=${DB_SECRET_ARN}" \
    "S3BucketName=${VAULT_FILES_BUCKET}" \
    "DownloadParseImageUri=${ECR_DP_URI}:${IMAGE_TAG}" \
    "EmbedImageUri=${ECR_EMBED_URI}:${IMAGE_TAG}"

# Get real ARNs
FN_VALIDATE_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "FnValidateArn")
FN_DOWNLOAD_PARSE_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "FnDownloadParseArn")
FN_CHUNK_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "FnChunkArn")
FN_STORE_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "FnStoreArn")
ECS_CLUSTER_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "EcsClusterArn")
EMBED_TASKDEF_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "EmbedTaskDefinitionArn")

echo "   fn-validate:      ${FN_VALIDATE_ARN}"
echo "   fn-chunk:         ${FN_CHUNK_ARN}"
echo "   fn-store:         ${FN_STORE_ARN}"
echo "   fn-download-parse: ${FN_DOWNLOAD_PARSE_ARN}"
echo "   ECS cluster:      ${ECS_CLUSTER_ARN}"
echo ""

# ============================================================================
# Step 4: Build zip Lambdas and update code
# ============================================================================
echo "=== Step 4/6: Build & Push Zip Lambda Code ==="

# 4a. fn-validate — zip and update
echo "   Building fn-validate..."
cd "${INDEXER_DIR}/lambdas/fn_validate"
zip -q -j /tmp/fn-validate.zip handler.py
aws lambda update-function-code \
    --function-name "${PROJECT}-fn-validate-${ENV}" \
    --zip-file "fileb:///tmp/fn-validate.zip" \
    --region "${REGION}" --no-cli-pager
echo "   Updated fn-validate"

# 4b. fn-chunk — zip with tiktoken (needs pip install into package dir)
echo "   Building fn-chunk..."
cd "${INDEXER_DIR}/lambdas/fn_chunk"
rm -rf /tmp/fn-chunk-pkg
mkdir -p /tmp/fn-chunk-pkg
cp handler.py /tmp/fn-chunk-pkg/
install_lambda_deps /tmp/fn-chunk-pkg tiktoken
cd /tmp/fn-chunk-pkg && zip -qr /tmp/fn-chunk.zip .
aws lambda update-function-code \
    --function-name "${PROJECT}-fn-chunk-${ENV}" \
    --zip-file "fileb:///tmp/fn-chunk.zip" \
    --region "${REGION}" --no-cli-pager
echo "   Updated fn-chunk"

# 4c. fn-store — zip with psycopg2 + pgvector
echo "   Building fn-store..."
cd "${INDEXER_DIR}/lambdas/fn_store"
rm -rf /tmp/fn-store-pkg
mkdir -p /tmp/fn-store-pkg
cp handler.py /tmp/fn-store-pkg/
install_lambda_deps /tmp/fn-store-pkg psycopg2-binary pgvector
cd /tmp/fn-store-pkg && zip -qr /tmp/fn-store.zip .
aws lambda update-function-code \
    --function-name "${PROJECT}-fn-store-${ENV}" \
    --zip-file "fileb:///tmp/fn-store.zip" \
    --region "${REGION}" --no-cli-pager
echo "   Updated fn-store"

echo "   Container images were built and wired through CloudFormation with tag ${IMAGE_TAG}"
echo ""

# ============================================================================
# Step 5: Deploy Step Functions (with real ARNs)
# ============================================================================
echo "=== Step 5/6: Step Functions ==="
deploy_stack "${PROJECT}-sfn-pipeline-${ENV}" "step-functions-pipeline.yml" \
    "ProjectPrefix=${PROJECT}" \
    "Environment=${ENV}" \
    "FnValidateArn=${FN_VALIDATE_ARN}" \
    "FnDownloadParseArn=${FN_DOWNLOAD_PARSE_ARN}" \
    "FnChunkArn=${FN_CHUNK_ARN}" \
    "FnStoreArn=${FN_STORE_ARN}" \
    "EmbedQueueArn=${EMBED_QUEUE_ARN}" \
    "EmbedQueueUrl=${EMBED_QUEUE_URL}" \
    "EcsClusterArn=${ECS_CLUSTER_ARN}" \
    "EmbedTaskDefArn=${EMBED_TASKDEF_ARN}" \
    "PrivateSubnetA=${SUBNET_A}" \
    "PrivateSubnetB=${SUBNET_B}" \
    "LambdaSecurityGroup=${LAMBDA_SG}"

SFN_ARN=$(get_output "${PROJECT}-sfn-pipeline-${ENV}" "StateMachineArn")
echo "   State Machine: ${SFN_ARN}"

echo "=== Step 5b/6: Re-point SQS starter to real State Machine ==="
deploy_stack "${PROJECT}-pipeline-lambdas-${ENV}" "pipeline-lambdas.yml" \
    "ProjectPrefix=${PROJECT}" \
    "Environment=${ENV}" \
    "VpcId=${VPC_ID}" \
    "PrivateSubnetA=${SUBNET_A}" \
    "PrivateSubnetB=${SUBNET_B}" \
    "LambdaSecurityGroup=${LAMBDA_SG}" \
    "IndexQueueArn=${SQS_ARN}" \
    "IndexQueueUrl=${SQS_URL}" \
    "EmbedQueueArn=${EMBED_QUEUE_ARN}" \
    "EmbedQueueUrl=${EMBED_QUEUE_URL}" \
    "EmbedQueueName=${EMBED_QUEUE_NAME}" \
    "EmbedDesiredCount=${EMBED_DESIRED_COUNT}" \
    "EmbedMaxCapacity=${EMBED_MAX_CAPACITY}" \
    "StateMachineArn=${SFN_ARN}" \
    "DbSecretArn=${DB_SECRET_ARN}" \
    "S3BucketName=${VAULT_FILES_BUCKET}" \
    "DownloadParseImageUri=${ECR_DP_URI}:${IMAGE_TAG}" \
    "EmbedImageUri=${ECR_EMBED_URI}:${IMAGE_TAG}"

# ============================================================================
# Step 6: Deploy Notifications
# ============================================================================
echo "=== Step 6/6: EventBridge + SNS ==="
deploy_stack "${PROJECT}-notifications-pipeline-${ENV}" "notifications-pipeline.yml" \
    "ProjectPrefix=${PROJECT}" \
    "Environment=${ENV}" \
    "StateMachineArn=${SFN_ARN}"

# ============================================================================
# Summary
# ============================================================================
echo ""
echo "============================================"
echo "  DEPLOYMENT COMPLETE"
echo "============================================"
echo ""
echo "  SQS Queue:    ${SQS_URL}"
echo "  Embed Queue:  ${EMBED_QUEUE_URL}"
echo "  State Machine: ${SFN_ARN}"
echo "  fn-validate:   ${FN_VALIDATE_ARN}"
echo "  fn-chunk:      ${FN_CHUNK_ARN}"
echo "  fn-store:      ${FN_STORE_ARN}"
echo "  fn-download-parse: ${FN_DOWNLOAD_PARSE_ARN}"
echo "  ECS Cluster:   ${ECS_CLUSTER_ARN}"
echo "  Embed Service: ${PROJECT}-embed-worker-${ENV} (desired=${EMBED_DESIRED_COUNT})"
echo ""
echo "  NEXT: Upload dataset to test:"
echo "    python services/indexer/scripts/upload_dataset.py \\"
echo "      --bucket ${VAULT_FILES_BUCKET} --no-endpoint \\"
echo "      --trigger --sfn-arn ${SFN_ARN}"
echo ""
