import type { APIGatewayProxyEvent } from "aws-lambda";
import { ApiError } from "./errors.js";

export interface AuthContext {
  user_id: string;
  email?: string;
}

// API Gateway's Cognito authorizer puts JWT claims under
// requestContext.authorizer.claims. `sub` is the Cognito user ID and serves
// as our users.id PK.
export function requireAuth(event: APIGatewayProxyEvent): AuthContext {
  const claims = event.requestContext.authorizer?.claims as
    | Record<string, string>
    | undefined;

  const sub = claims?.sub;
  if (!sub) {
    throw ApiError.unauthorized();
  }

  return {
    user_id: sub,
    email: claims?.email
  };
}
