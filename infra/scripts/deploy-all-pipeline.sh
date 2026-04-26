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
#   6. Deploy notifications stack (EventBridge + SNS)
#   7. Print summary
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

echo "============================================"
echo "  Solo Vault Pipeline Deploy"
echo "  Environment: ${ENV}"
echo "  Account:     ${ACCOUNT_ID}"
echo "  Region:      ${REGION}"
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
        aws cloudformation update-stack \
            --stack-name "${STACK_NAME}" \
            --template-body "file://${CFN_DIR}/${TEMPLATE}" \
            --parameters ${PARAM_OVERRIDES} \
            --capabilities CAPABILITY_NAMED_IAM \
            --region "${REGION}" 2>/dev/null || {
                echo "   No changes (or error). Continuing..."
                return 0
            }
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

# ============================================================================
# Step 1: Deploy SQS
# ============================================================================
echo "=== Step 1/6: SQS Queue ==="
deploy_stack "${PROJECT}-sqs-pipeline-${ENV}" "sqs-pipeline.yml" \
    "ProjectPrefix=${PROJECT}" \
    "Environment=${ENV}"

SQS_ARN=$(get_output "${PROJECT}-sqs-pipeline-${ENV}" "IndexQueueArn")
SQS_URL=$(get_output "${PROJECT}-sqs-pipeline-${ENV}" "IndexQueueUrl")
echo "   SQS ARN: ${SQS_ARN}"

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

# ============================================================================
# Step 3: Deploy Lambda + ECS + IAM stack (creates functions with placeholder code)
# ============================================================================
echo "=== Step 3/6: Lambda Functions + ECS + IAM ==="

# First push a dummy image to ECR so CloudFormation can reference it
# (ECR repos are created by this stack, so we need to create the stack
#  with placeholder code first, then push real images)

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
    "StateMachineArn=${SFN_ARN_PLACEHOLDER}" \
    "S3BucketName=${PROJECT}-vault-${ENV}"

# Get real ARNs
FN_VALIDATE_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "FnValidateArn")
FN_DOWNLOAD_PARSE_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "FnDownloadParseArn")
FN_CHUNK_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "FnChunkArn")
FN_STORE_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "FnStoreArn")
ECS_CLUSTER_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "EcsClusterArn")
EMBED_TASKDEF_ARN=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "EmbedTaskDefinitionArn")
ECR_DP_URI=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "EcrDownloadParseUri")
ECR_EMBED_URI=$(get_output "${PROJECT}-pipeline-lambdas-${ENV}" "EcrEmbedUri")

echo "   fn-validate:      ${FN_VALIDATE_ARN}"
echo "   fn-chunk:         ${FN_CHUNK_ARN}"
echo "   fn-store:         ${FN_STORE_ARN}"
echo "   fn-download-parse: ${FN_DOWNLOAD_PARSE_ARN}"
echo "   ECS cluster:      ${ECS_CLUSTER_ARN}"
echo ""

# ============================================================================
# Step 4: Build + push Docker images, update Lambda code
# ============================================================================
echo "=== Step 4/6: Build & Push Code ==="

# Login to ECR
aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

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
pip install --quiet --target /tmp/fn-chunk-pkg tiktoken 2>/dev/null
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
pip install --quiet --target /tmp/fn-store-pkg psycopg2-binary pgvector 2>/dev/null
cd /tmp/fn-store-pkg && zip -qr /tmp/fn-store.zip .
aws lambda update-function-code \
    --function-name "${PROJECT}-fn-store-${ENV}" \
    --zip-file "fileb:///tmp/fn-store.zip" \
    --region "${REGION}" --no-cli-pager
echo "   Updated fn-store"

# 4d. fn-download-parse — Docker container image
echo "   Building fn-download-parse (Docker)..."
cd "${INDEXER_DIR}/lambdas/fn_download_parse"
# Copy app/ package for parser imports
cp -r "${INDEXER_DIR}/app" ./app/
docker build -t "${PROJECT}-fn-download-parse:latest" . --quiet
docker tag "${PROJECT}-fn-download-parse:latest" "${ECR_DP_URI}:latest"
docker push "${ECR_DP_URI}:latest" --quiet 2>/dev/null || docker push "${ECR_DP_URI}:latest"
rm -rf ./app/  # cleanup copied app
aws lambda update-function-code \
    --function-name "${PROJECT}-fn-download-parse-${ENV}" \
    --image-uri "${ECR_DP_URI}:latest" \
    --region "${REGION}" --no-cli-pager
echo "   Updated fn-download-parse"

# 4e. fn-embed — Docker container image (ECS)
echo "   Building fn-embed (Docker — this may take a while for BGE-M3 download)..."
cd "${INDEXER_DIR}/lambdas/fn_embed"
docker build -t "${PROJECT}-fn-embed:latest" . --quiet 2>/dev/null || docker build -t "${PROJECT}-fn-embed:latest" .
docker tag "${PROJECT}-fn-embed:latest" "${ECR_EMBED_URI}:latest"
docker push "${ECR_EMBED_URI}:latest" --quiet 2>/dev/null || docker push "${ECR_EMBED_URI}:latest"
echo "   Updated fn-embed"
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
    "EcsClusterArn=${ECS_CLUSTER_ARN}" \
    "EmbedTaskDefArn=${EMBED_TASKDEF_ARN}" \
    "PrivateSubnets=${SUBNET_A}" \
    "LambdaSecurityGroup=${LAMBDA_SG}"

SFN_ARN=$(get_output "${PROJECT}-sfn-pipeline-${ENV}" "StateMachineArn")
echo "   State Machine: ${SFN_ARN}"

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
echo "  State Machine: ${SFN_ARN}"
echo "  fn-validate:   ${FN_VALIDATE_ARN}"
echo "  fn-chunk:      ${FN_CHUNK_ARN}"
echo "  fn-store:      ${FN_STORE_ARN}"
echo "  fn-download-parse: ${FN_DOWNLOAD_PARSE_ARN}"
echo "  ECS Cluster:   ${ECS_CLUSTER_ARN}"
echo ""
echo "  NEXT: Upload dataset to test:"
echo "    python services/indexer/scripts/upload_dataset.py \\"
echo "      --bucket ${PROJECT}-vault-${ENV} --no-endpoint \\"
echo "      --trigger --sfn-arn ${SFN_ARN}"
echo ""
