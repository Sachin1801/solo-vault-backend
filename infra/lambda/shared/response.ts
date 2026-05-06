import type { APIGatewayProxyResult } from "aws-lambda";
import { ApiError, ErrorCode } from "./errors.js";

// CORS open in dev. Tighten to Tauri origins before staging:
//   tauri://localhost, https://tauri.localhost, http://localhost:*
const JSON_HEADERS = {
  "Content-Type": "application/json",
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Authorization,Content-Type",
  "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS"
};

export function ok<T>(body: T, status = 200): APIGatewayProxyResult {
  return {
    statusCode: status,
    headers: JSON_HEADERS,
    body: JSON.stringify(body)
  };
}

export function created<T>(body: T): APIGatewayProxyResult {
  return ok(body, 201);
}

export function error(
  status: number,
  code: ErrorCode,
  message: string
): APIGatewayProxyResult {
  return {
    statusCode: status,
    headers: JSON_HEADERS,
    body: JSON.stringify({ error: { code, message } })
  };
}

export function handleError(err: unknown): APIGatewayProxyResult {
  if (err instanceof ApiError) {
    return error(err.status, err.code, err.message);
  }
  console.error("Unhandled error:", err);
  return error(500, ErrorCode.INTERNAL_ERROR, "Unexpected server error");
}
