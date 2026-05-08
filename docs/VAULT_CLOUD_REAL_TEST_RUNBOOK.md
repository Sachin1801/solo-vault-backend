# Vault Cloud Real Test Runbook

## Current Environments

- Dev API: `https://tlrskvdxe2.execute-api.us-east-1.amazonaws.com/prod`
- Prod API: `https://vt1fr7hb50.execute-api.us-east-1.amazonaws.com/prod`
- Dev bucket: `solo-vault-dev-vault-files-465443875827`
- Prod bucket: `solo-vault-prod-vault-files-465443875827`
- Dev state machine: `arn:aws:states:us-east-1:465443875827:stateMachine:solo-vault-index-pipeline-dev`
- Prod state machine: `arn:aws:states:us-east-1:465443875827:stateMachine:solo-vault-index-pipeline-prod`

Indexing is intentionally triggered by authenticated `POST /vault/entries/{id}/upload/complete`, not by S3 `ObjectCreated` notifications.

## Where Changes Should Appear In AWS

After a desktop or API upload succeeds, expect these places to change:

- API Gateway: the prod/dev REST API receives `POST /vault/entries`, `POST /vault/entries/{id}/upload`, and `POST /vault/entries/{id}/upload/complete`.
- CRUD Lambda logs: `/aws/lambda/solo-vault-{dev|prod}-vault-crud` should show upload URL creation, S3 `HEAD`, and Step Functions start events for the entry id.
- S3 user object: `s3://solo-vault-{dev|prod}-vault-files-465443875827/users/{cognito_sub}/entries/{entry_id}/objects/{safe_filename}` should exist after the PUT.
- RDS entry row: `vault.entries.vault_blob_path`, `cloud_sync_state`, `index_status`, `uploaded_at`, `updated_at`, `size_bytes`, and `mime` should update after upload completion.
- Step Functions: `solo-vault-index-pipeline-{dev|prod}` should have one execution whose input includes the entry id, user id, bucket, and S3 key.
- Pipeline Lambda logs:
  - `/aws/lambda/solo-vault-fn-validate-{dev|prod}`
  - `/aws/lambda/solo-vault-fn-download-parse-{dev|prod}`
  - `/aws/lambda/solo-vault-fn-chunk-{dev|prod}`
  - `/aws/lambda/solo-vault-fn-store-{dev|prod}`
- ECS embed logs: `/ecs/solo-vault-fn-embed-{dev|prod}` should show the embedding task reading chunks and writing embeddings.
- S3 pipeline scratch: `s3://.../pipeline/{entry_id}/text.json`, `chunks.json`, or `embeddings.json` may appear while indexing runs. After store succeeds, the store Lambda normally deletes that scratch prefix.
- RDS index tables: `vault.chunks` and `vault.chunks_fts` should have rows for the entry id after successful indexing.
- Delete flow: `DELETE /vault/entries/{id}` should remove `vault.chunks_fts`, `vault.chunks`, the `vault.entries` row, and the S3 object at `vault_blob_path`.

There is no required S3 `ObjectCreated` notification in the current design. The trusted trigger is the authenticated `upload/complete` API call.

## One Command Debug Report

After a failed upload/index/delete, run this from the backend repo and paste the full output:

```bash
cd /Users/sachin/Developer/Orbit_Main/solo-vault-backend
AWS_PROFILE=solo infra/scripts/vault-debug-report.sh dev "$ENTRY_ID" "$COGNITO_SUB"
AWS_PROFILE=solo infra/scripts/vault-debug-report.sh prod "$ENTRY_ID" "$COGNITO_SUB"
```

If you do not know the Cognito sub, omit the third argument. The script will try to read it from RDS when either `psql` or the repo's Node `pg` package can reach the database:

```bash
AWS_PROFILE=solo infra/scripts/vault-debug-report.sh dev "$ENTRY_ID"
```

The report includes CloudFormation outputs, API routes, Lambda config timestamps, RDS row/chunk counts when reachable, S3 object prefixes, Step Functions execution history, ECS task state, and CloudWatch log lines filtered by entry id. It does not print tokens, signed URLs, or DB passwords. If the RDS section shows a connection timeout, use the rest of the report and then check the row from DataGrip or temporarily allow the current testing IP.

## Prerequisites

- `AWS_PROFILE=solo`
- A valid Cognito ID token for the environment being tested.
- `curl`, `jq`, and AWS CLI.
- A small text/PDF fixture that is safe to store in dev/prod.

## API Flow

Set environment variables:

```bash
export API_URL="https://tlrskvdxe2.execute-api.us-east-1.amazonaws.com/prod"
export STATE_MACHINE_ARN="arn:aws:states:us-east-1:465443875827:stateMachine:solo-vault-index-pipeline-dev"
export TOKEN="<cognito-id-token>"
export ENTRY_ID="vault-test-$(date +%s)"
export FIXTURE="/absolute/path/to/test-file.txt"
export NOW="$(date +%s)"
```

Create an entry:

```bash
curl -sS -X POST "$API_URL/vault/entries" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"id\":\"$ENTRY_ID\",
    \"kind\":\"document\",
    \"subkind\":\"txt\",
    \"title\":\"Vault cloud smoke test\",
    \"content\":null,
    \"source_path\":null,
    \"vault_blob_path\":null,
    \"scope_type\":\"global\",
    \"memory_type\":\"user\",
    \"pinned\":0,
    \"tags\":\"[\\\"smoke\\\"]\",
    \"mime\":\"text/plain\",
    \"size_bytes\":null,
    \"index_status\":\"pending\",
    \"cloud_sync_state\":\"pending\",
    \"classifier_confidence\":1,
    \"created_at\":$NOW,
    \"updated_at\":$NOW
  }" | jq .
```

Request a signed upload URL:

```bash
UPLOAD_RESPONSE="$(curl -sS -X POST "$API_URL/vault/entries/$ENTRY_ID/upload" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"filename\":\"$(basename "$FIXTURE")\",\"content_type\":\"text/plain\"}")"
echo "$UPLOAD_RESPONSE" | jq .
UPLOAD_URL="$(echo "$UPLOAD_RESPONSE" | jq -r .upload_url)"
S3_KEY="$(echo "$UPLOAD_RESPONSE" | jq -r .s3_key)"
```

Verify key format before upload:

```bash
echo "$S3_KEY"
# Expected: users/{cognito_sub}/entries/{entry_id}/objects/{safe_filename}
```

PUT the file to S3:

```bash
curl -sS -X PUT "$UPLOAD_URL" \
  -H "Content-Type: text/plain" \
  --data-binary "@$FIXTURE"
```

Complete the upload and start indexing:

```bash
curl -sS -X POST "$API_URL/vault/entries/$ENTRY_ID/upload/complete" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"s3_key\":\"$S3_KEY\"}" | jq .
```

Watch Step Functions:

```bash
aws stepfunctions list-executions \
  --state-machine-arn "$STATE_MACHINE_ARN" \
  --status-filter RUNNING \
  --max-results 5
```

Check entry status:

```bash
curl -sS "$API_URL/vault/entries/$ENTRY_ID" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Search:

```bash
curl -sS -X POST "$API_URL/vault/search" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"query\":\"smoke\",\"limit\":10}" | jq .
```

Download:

```bash
curl -sS "$API_URL/vault/entries/$ENTRY_ID/download" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

Delete:

```bash
curl -sS -X DELETE "$API_URL/vault/entries/$ENTRY_ID" \
  -H "Authorization: Bearer $TOKEN" | jq .
```

## Authorization Tests

- Repeat get/download/search/delete with a second user's token; the entry must not be returned.
- Verify viewer/editor/owner behavior after sharing APIs are added.
- Confirm no user receives direct S3 permissions; all access is via signed URLs from the API.

## Desktop Tests

- Upload from the desktop app and confirm local state moves `pending -> uploading -> indexing_remote -> synced`.
- Kill and restart the app while the remote index is running; it should recover by polling remote state.
- Delete with `also_remote=true`; confirm the entry, chunks, FTS rows, and S3 object are removed.
- Force an index failure with an unsupported/corrupt file and confirm local state becomes `failed` with a visible reason.
