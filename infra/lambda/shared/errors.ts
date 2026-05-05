export const ErrorCode = {
  INVALID_INPUT: "INVALID_INPUT",
  UNAUTHORIZED: "UNAUTHORIZED",
  FORBIDDEN: "FORBIDDEN",
  ENTRY_NOT_FOUND: "ENTRY_NOT_FOUND",
  INTERNAL_ERROR: "INTERNAL_ERROR",
} as const;

export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode];

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: ErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  static invalidInput(message: string): ApiError {
    return new ApiError(400, ErrorCode.INVALID_INPUT, message);
  }

  static unauthorized(message = "Missing or invalid Cognito JWT"): ApiError {
    return new ApiError(401, ErrorCode.UNAUTHORIZED, message);
  }

  static forbidden(message = "User does not own this resource"): ApiError {
    return new ApiError(403, ErrorCode.FORBIDDEN, message);
  }

  static entryNotFound(id: string): ApiError {
    return new ApiError(
      404,
      ErrorCode.ENTRY_NOT_FOUND,
      `Vault entry with id '${id}' not found`,
    );
  }

  static internal(message = "Unexpected server error"): ApiError {
    return new ApiError(500, ErrorCode.INTERNAL_ERROR, message);
  }
}
