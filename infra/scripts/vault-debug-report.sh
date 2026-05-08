#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  AWS_PROFILE=solo infra/scripts/vault-debug-report.sh <dev|prod> <entry_id> [cognito_user_sub]

Optional environment:
  AWS_REGION=us-east-1
  VAULT_DEBUG_MINUTES=180
  VAULT_DEBUG_LOG_MAX=80

This prints a copy/paste-safe report for one Vault entry. It does not print
Cognito tokens, signed URLs, or database passwords.
USAGE
}

ENVIRONMENT="${1:-}"
ENTRY_ID="${2:-}"
USER_ID="${3:-}"

if [[ -z "${ENVIRONMENT}" || -z "${ENTRY_ID}" ]]; then
  usage
  exit 2
fi

REGION="${AWS_REGION:-us-east-1}"
PROJECT="solo-vault"
LOOKBACK_MINUTES="${VAULT_DEBUG_MINUTES:-180}"
LOG_MAX="${VAULT_DEBUG_LOG_MAX:-80}"
START_MS=$(( ($(date +%s) - LOOKBACK_MINUTES * 60) * 1000 ))

AWS_ARGS=(--region "${REGION}" --no-cli-pager)
if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS_ARGS=(--profile "${AWS_PROFILE}" "${AWS_ARGS[@]}")
fi

aws_cli() {
  aws "${AWS_ARGS[@]}" "$@"
}

section() {
  printf '\n========== %s ==========\n' "$1"
}

kv() {
  printf '%-28s %s\n' "$1:" "${2:-<missing>}"
}

get_export() {
  local export_name="$1"
  aws_cli cloudformation list-exports \
    --query "Exports[?Name=='${export_name}'].Value | [0]" \
    --output text 2>/dev/null \
    | awk 'NF && $0 != "None" { print; exit }'
}

json_or_raw() {
  if command -v jq >/dev/null 2>&1; then
    jq .
  else
    cat
  fi
}

log_group_exists() {
  local group="$1"
  [[ "$(aws_cli logs describe-log-groups \
      --log-group-name-prefix "${group}" \
      --query "logGroups[?logGroupName=='${group}'].logGroupName | [0]" \
      --output text 2>/dev/null || true)" == "${group}" ]]
}

filter_log_group() {
  local group="$1"
  section "CloudWatch ${group}"
  if ! log_group_exists "${group}"; then
    echo "Log group does not exist yet."
    return 0
  fi

  local output
  if ! output="$(aws_cli logs filter-log-events \
      --log-group-name "${group}" \
      --start-time "${START_MS}" \
      --filter-pattern "\"${ENTRY_ID}\"" \
      --max-items "${LOG_MAX}" \
      --output json 2>&1)"; then
    echo "${output}"
    return 0
  fi

  if command -v jq >/dev/null 2>&1; then
    local count
    count="$(jq '.events | length' <<<"${output}")"
    if [[ "${count}" == "0" ]]; then
      echo "No log events matched entry_id=${ENTRY_ID} in the last ${LOOKBACK_MINUTES} minutes."
      return 0
    fi
    jq -r '.events[]
      | "time=\((.timestamp / 1000) | todateiso8601) stream=\(.logStreamName)\n\(.message)\n---"' <<<"${output}"
  else
    echo "${output}"
  fi
}

section "Identity"
ACCOUNT_ID="$(aws_cli sts get-caller-identity --query Account --output text)"
CALLER_ARN="$(aws_cli sts get-caller-identity --query Arn --output text)"
kv "aws_profile" "${AWS_PROFILE:-<default>}"
kv "account_id" "${ACCOUNT_ID}"
kv "caller_arn" "${CALLER_ARN}"
kv "region" "${REGION}"
kv "environment" "${ENVIRONMENT}"
kv "entry_id" "${ENTRY_ID}"
kv "user_id_arg" "${USER_ID:-<not provided>}"
kv "lookback_minutes" "${LOOKBACK_MINUTES}"

API_ID="$(get_export "${PROJECT}-${ENVIRONMENT}-api-id")"
API_URL="$(get_export "${PROJECT}-${ENVIRONMENT}-api-url")"
BUCKET="$(get_export "${PROJECT}-${ENVIRONMENT}-vault-files-bucket")"
STATE_MACHINE_ARN="$(get_export "${PROJECT}-sfn-pipeline-arn-${ENVIRONMENT}")"
DB_SECRET_ARN="$(get_export "${PROJECT}-${ENVIRONMENT}-db-credentials-arn")"
DB_HOST="$(get_export "${PROJECT}-${ENVIRONMENT}-rds-endpoint")"
DB_PORT="$(get_export "${PROJECT}-${ENVIRONMENT}-rds-port")"
DB_NAME="$(get_export "${PROJECT}-${ENVIRONMENT}-rds-dbname")"
CRUD_FUNCTION="$(get_export "${PROJECT}-${ENVIRONMENT}-vault-crud-name")"
ECS_CLUSTER_ARN="$(get_export "${PROJECT}-embed-cluster-arn-${ENVIRONMENT}")"

section "Stack Outputs"
kv "api_id" "${API_ID}"
kv "api_url" "${API_URL}"
kv "bucket" "${BUCKET}"
kv "state_machine_arn" "${STATE_MACHINE_ARN}"
kv "crud_function" "${CRUD_FUNCTION}"
kv "db_host" "${DB_HOST}"
kv "db_port" "${DB_PORT}"
kv "db_name" "${DB_NAME}"
kv "db_secret_arn" "${DB_SECRET_ARN}"
kv "ecs_cluster_arn" "${ECS_CLUSTER_ARN}"

section "API Gateway Vault Routes"
if [[ -n "${API_ID}" ]]; then
  aws_cli apigateway get-resources \
    --rest-api-id "${API_ID}" \
    --query "items[?contains(path, '/vault')].{path:path,methods:resourceMethods}" \
    --output json | json_or_raw || true
else
  echo "API id export was not found."
fi

section "Lambda Configuration"
for fn in \
  "solo-vault-${ENVIRONMENT}-vault-crud" \
  "solo-vault-fn-validate-${ENVIRONMENT}" \
  "solo-vault-fn-download-parse-${ENVIRONMENT}" \
  "solo-vault-fn-chunk-${ENVIRONMENT}" \
  "solo-vault-fn-store-${ENVIRONMENT}"
do
  echo "--- ${fn}"
  aws_cli lambda get-function-configuration \
    --function-name "${fn}" \
    --query '{FunctionName:FunctionName,LastModified:LastModified,State:State,PackageType:PackageType,Runtime:Runtime,Timeout:Timeout,MemorySize:MemorySize}' \
    --output json 2>/dev/null | json_or_raw || echo "Missing function or no permission."
done

DB_USER_ID=""
section "RDS Entry State"
if command -v jq >/dev/null 2>&1 && [[ -n "${DB_SECRET_ARN}" ]]; then
  SECRET_JSON="$(aws_cli secretsmanager get-secret-value \
    --secret-id "${DB_SECRET_ARN}" \
    --query SecretString \
    --output text)"
  DB_USER="$(jq -r '.username' <<<"${SECRET_JSON}")"
  DB_PASSWORD="$(jq -r '.password' <<<"${SECRET_JSON}")"
  DB_HOST_FROM_SECRET="$(jq -r '.host // empty' <<<"${SECRET_JSON}")"
  DB_PORT_FROM_SECRET="$(jq -r '.port // empty' <<<"${SECRET_JSON}")"
  DB_NAME_FROM_SECRET="$(jq -r '.dbname // empty' <<<"${SECRET_JSON}")"
  DB_HOST="${DB_HOST_FROM_SECRET:-${DB_HOST}}"
  DB_PORT="${DB_PORT_FROM_SECRET:-${DB_PORT:-5432}}"
  DB_NAME="${DB_NAME_FROM_SECRET:-${DB_NAME}}"

  if command -v psql >/dev/null 2>&1; then
    DB_USER_ID="$(PGPASSWORD="${DB_PASSWORD}" psql \
      "host=${DB_HOST} port=${DB_PORT} dbname=${DB_NAME} user=${DB_USER} sslmode=require" \
      -v ON_ERROR_STOP=1 \
      -v entry_id="${ENTRY_ID}" \
      -At \
      -c "SELECT user_id FROM vault.entries WHERE id = :'entry_id' LIMIT 1;" 2>/dev/null || true)"

    PGPASSWORD="${DB_PASSWORD}" psql \
      "host=${DB_HOST} port=${DB_PORT} dbname=${DB_NAME} user=${DB_USER} sslmode=require" \
      -v ON_ERROR_STOP=1 \
      -v entry_id="${ENTRY_ID}" \
      -x \
      -c "SELECT id, user_id, owner_user_id, vault_blob_path, cloud_sync_state, index_status, chunk_count, file_hash, embedding_model, chunker_version, index_error, uploaded_at, indexed_at, updated_at FROM vault.entries WHERE id = :'entry_id';" \
      -c "SELECT count(*) AS chunk_rows FROM vault.chunks WHERE entry_id = :'entry_id';" \
      -c "SELECT count(*) AS fts_rows FROM vault.chunks_fts WHERE entry_id = :'entry_id';" || true
  elif command -v node >/dev/null 2>&1 && node -e "require.resolve('pg')" >/dev/null 2>&1; then
    NODE_DB_REPORT="$(DB_SECRET_JSON="${SECRET_JSON}" ENTRY_ID="${ENTRY_ID}" node <<'NODE'
const { Client } = require("pg");

(async () => {
  const secret = JSON.parse(process.env.DB_SECRET_JSON);
  const entryId = process.env.ENTRY_ID;
  const client = new Client({
    host: secret.host,
    port: Number(secret.port || 5432),
    database: secret.dbname,
    user: secret.username,
    password: secret.password,
    ssl: { rejectUnauthorized: false },
    connectionTimeoutMillis: 10000,
  });
  await client.connect();
  try {
    const entry = await client.query(
      `SELECT id, user_id, owner_user_id, vault_blob_path, cloud_sync_state,
              index_status, chunk_count, file_hash, embedding_model,
              chunker_version, index_error, uploaded_at, indexed_at, updated_at
         FROM vault.entries
        WHERE id = $1`,
      [entryId],
    );
    const chunks = await client.query(
      "SELECT count(*)::int AS chunk_rows FROM vault.chunks WHERE entry_id = $1",
      [entryId],
    );
    const fts = await client.query(
      "SELECT count(*)::int AS fts_rows FROM vault.chunks_fts WHERE entry_id = $1",
      [entryId],
    );
    console.log(JSON.stringify({
      entry: entry.rows[0] || null,
      chunk_rows: chunks.rows[0]?.chunk_rows || 0,
      fts_rows: fts.rows[0]?.fts_rows || 0,
    }, null, 2));
  } finally {
    await client.end();
  }
})().catch((error) => {
  console.log(JSON.stringify({ error: error.message }, null, 2));
  process.exitCode = 0;
});
NODE
)"
    echo "${NODE_DB_REPORT}"
    DB_USER_ID="$(jq -r '.entry.user_id // empty' <<<"${NODE_DB_REPORT}")"
  else
    echo "Skipping DB query. Requires psql, or node with the pg package installed."
  fi
else
  echo "Skipping DB query. Requires jq and the ${PROJECT}-${ENVIRONMENT}-db-credentials-arn export."
fi

if [[ -z "${USER_ID}" && -n "${DB_USER_ID}" ]]; then
  USER_ID="${DB_USER_ID}"
fi

section "S3 Objects"
if [[ -z "${BUCKET}" ]]; then
  echo "Bucket export was not found."
else
  if [[ -n "${USER_ID}" ]]; then
    echo "--- user object prefix: users/${USER_ID}/entries/${ENTRY_ID}/"
    aws_cli s3api list-objects-v2 \
      --bucket "${BUCKET}" \
      --prefix "users/${USER_ID}/entries/${ENTRY_ID}/" \
      --max-items 50 \
      --query 'Contents[].{Key:Key,Size:Size,LastModified:LastModified,ETag:ETag}' \
      --output json | json_or_raw || true
  else
    echo "User id was not provided and could not be read from RDS, so the exact user object prefix cannot be checked."
  fi
  echo "--- pipeline scratch prefix: pipeline/${ENTRY_ID}/"
  aws_cli s3api list-objects-v2 \
    --bucket "${BUCKET}" \
    --prefix "pipeline/${ENTRY_ID}/" \
    --max-items 50 \
    --query 'Contents[].{Key:Key,Size:Size,LastModified:LastModified,ETag:ETag}' \
    --output json | json_or_raw || true
fi

section "Step Functions"
if [[ -n "${STATE_MACHINE_ARN}" ]]; then
  EXECUTIONS_JSON="$(aws_cli stepfunctions list-executions \
    --state-machine-arn "${STATE_MACHINE_ARN}" \
    --max-results 25 \
    --output json)"
  if command -v jq >/dev/null 2>&1; then
    echo "--- recent executions"
    jq -r '.executions[]
      | "\(.startDate) status=\(.status) name=\(.name) arn=\(.executionArn)"' <<<"${EXECUTIONS_JSON}"

    MATCH_FILE="$(mktemp)"
    trap 'rm -f "${MATCH_FILE}"' EXIT
    jq -r '.executions[].executionArn' <<<"${EXECUTIONS_JSON}" | while read -r arn; do
      input="$(aws_cli stepfunctions describe-execution \
        --execution-arn "${arn}" \
        --query input \
        --output text 2>/dev/null || true)"
      if grep -Fq "${ENTRY_ID}" <<<"${input}"; then
        echo "${arn}" >> "${MATCH_FILE}"
      fi
    done

    if [[ ! -s "${MATCH_FILE}" ]]; then
      echo "--- no recent executions included entry_id=${ENTRY_ID}"
    else
      while read -r arn; do
        echo "--- matching execution: ${arn}"
        aws_cli stepfunctions describe-execution \
          --execution-arn "${arn}" \
          --output json | json_or_raw || true
        echo "--- recent execution history"
        aws_cli stepfunctions get-execution-history \
          --execution-arn "${arn}" \
          --max-results 40 \
          --reverse-order \
          --output json \
          | jq -r '.events[]
            | "id=\(.id) time=\(.timestamp) type=\(.type)\n\(.stateEnteredEventDetails // .stateExitedEventDetails // .lambdaFunctionFailedEventDetails // .taskFailedEventDetails // .executionFailedEventDetails // .taskSubmittedEventDetails // .taskSucceededEventDetails // {})\n---"' || true
      done < "${MATCH_FILE}"
    fi
  else
    echo "${EXECUTIONS_JSON}"
    echo "Install jq to match executions by entry id and print compact history."
  fi
else
  echo "State machine export was not found."
fi

section "ECS Embed Tasks"
if [[ -n "${ECS_CLUSTER_ARN}" ]]; then
  for desired in RUNNING STOPPED; do
    echo "--- ${desired}"
    TASKS="$(aws_cli ecs list-tasks \
      --cluster "${ECS_CLUSTER_ARN}" \
      --desired-status "${desired}" \
      --max-results 10 \
      --query 'taskArns' \
      --output text 2>/dev/null || true)"
    if [[ -z "${TASKS}" || "${TASKS}" == "None" ]]; then
      echo "No ${desired} tasks."
    else
      aws_cli ecs describe-tasks \
        --cluster "${ECS_CLUSTER_ARN}" \
        --tasks ${TASKS} \
        --query 'tasks[].{taskArn:taskArn,lastStatus:lastStatus,desiredStatus:desiredStatus,stoppedReason:stoppedReason,containers:containers[].{name:name,lastStatus:lastStatus,exitCode:exitCode,reason:reason}}' \
        --output json | json_or_raw || true
    fi
  done
else
  echo "ECS cluster export was not found."
fi

filter_log_group "/aws/lambda/solo-vault-${ENVIRONMENT}-vault-crud"
filter_log_group "/aws/lambda/solo-vault-fn-validate-${ENVIRONMENT}"
filter_log_group "/aws/lambda/solo-vault-fn-download-parse-${ENVIRONMENT}"
filter_log_group "/aws/lambda/solo-vault-fn-chunk-${ENVIRONMENT}"
filter_log_group "/aws/lambda/solo-vault-fn-store-${ENVIRONMENT}"
filter_log_group "/ecs/solo-vault-fn-embed-${ENVIRONMENT}"

section "Copy/Paste Summary"
cat <<SUMMARY
Environment: ${ENVIRONMENT}
Entry ID: ${ENTRY_ID}
User ID: ${USER_ID:-<unknown>}
API URL: ${API_URL:-<missing>}
Bucket: ${BUCKET:-<missing>}
State machine: ${STATE_MACHINE_ARN:-<missing>}

Expected success shape:
- RDS vault.entries cloud_sync_state=synced and index_status=indexed
- RDS vault.entries chunk_count > 0
- RDS vault.chunks rows > 0
- RDS vault.chunks_fts rows > 0
- S3 user object exists under users/{user_id}/entries/{entry_id}/objects/
- S3 pipeline/{entry_id}/ scratch objects are usually gone after store completes
- Step Functions execution status is SUCCEEDED
SUMMARY
