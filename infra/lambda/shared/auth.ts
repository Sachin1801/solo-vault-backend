import type { APIGatewayProxyEvent } from "aws-lambda";
import { ApiError } from "./errors.js";

export type AuthContext = {
  user_id: string;
  email: string;
};

// API Gateway injects Cognito JWT claims into event.requestContext.authorizer
// when the CognitoAuthorizer is attached to the route. Never trust a
// client-supplied user_id — always derive identity from these claims.
export function requireAuth(event: APIGatewayProxyEvent): AuthContext {
  const claims = (event.requestContext.authorizer as Record<string, string> | undefined)
    ?.claims as Record<string, string> | undefined;
  const sub = claims?.sub;
  if (!sub) {
    throw ApiError.unauthorized();
  }
  return {
    user_id: sub,
    email: claims?.email ?? `${sub}@unknown.local`,
  };
}
