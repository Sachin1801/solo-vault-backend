// Error taxonomy mirrored from docs/API.md. Each error has a stable code so
// clients can branch on it; HTTP status is fixed per error type.

export const ErrorCode = {
  INVALID_INPUT: "INVALID_INPUT",
  UNAUTHORIZED: "UNAUTHORIZED",
  INVALID_TOKEN: "INVALID_TOKEN",
  FORBIDDEN: "FORBIDDEN",
  ENTRY_NOT_FOUND: "ENTRY_NOT_FOUND",
  SESSION_NOT_FOUND: "SESSION_NOT_FOUND",
  QUOTA_EXCEEDED: "QUOTA_EXCEEDED",
  COGNITO_ERROR: "COGNITO_ERROR",
  INTERNAL_ERROR: "INTERNAL_ERROR"
} as const;

export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode];

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: ErrorCode,
    message: string
  ) {
    super(message);
    this.name = "ApiError";
  }

  static invalidInput(message: string) {
    return new ApiError(400, ErrorCode.INVALID_INPUT, message);
  }

  static unauthorized(message = "Missing or invalid Cognito JWT") {
    return new ApiError(401, ErrorCode.UNAUTHORIZED, message);
  }

  static forbidden(message = "User does not own this resource") {
    return new ApiError(403, ErrorCode.FORBIDDEN, message);
  }

  static entryNotFound(id: string) {
    return new ApiError(
      404,
      ErrorCode.ENTRY_NOT_FOUND,
      `Vault entry with id '${id}' not found`
    );
  }

  static internal(message = "Unexpected server error") {
    return new ApiError(500, ErrorCode.INTERNAL_ERROR, message);
  }
}
