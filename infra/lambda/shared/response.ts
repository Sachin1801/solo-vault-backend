import type { APIGatewayProxyResult } from "aws-lambda";
import { ApiError, ErrorCode } from "./errors.js";

const JSON_HEADERS = {
  "Content-Type": "application/json",
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Authorization,Content-Type",
  "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
};

export function ok(body: unknown, status = 200): APIGatewayProxyResult {
  return { statusCode: status, headers: JSON_HEADERS, body: JSON.stringify(body) };
}

export function created(body: unknown): APIGatewayProxyResult {
  return ok(body, 201);
}

function errorResponse(
  status: number,
  code: string,
  message: string,
): APIGatewayProxyResult {
  return {
    statusCode: status,
    headers: JSON_HEADERS,
    body: JSON.stringify({ error: { code, message } }),
  };
}

export function handleError(err: unknown): APIGatewayProxyResult {
  if (err instanceof ApiError) {
    return errorResponse(err.status, err.code, err.message);
  }
  console.error("Unhandled error:", err);
  return errorResponse(500, ErrorCode.INTERNAL_ERROR, "Unexpected server error");
}
