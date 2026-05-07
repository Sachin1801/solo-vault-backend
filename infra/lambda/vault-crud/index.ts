import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { handleError } from "../shared/response.js";
import { requireAuth } from "../shared/auth.js";
import { listEntries } from "./list.js";
import { createEntry } from "./create.js";
import { getEntry } from "./get.js";
import { updateEntry } from "./update.js";
import { deleteEntry } from "./delete.js";
import { completeUploadEntry, uploadEntry } from "./upload.js";
import { downloadEntry } from "./download.js";
import { searchEntries } from "./search.js";

export const handler = async (
  event: APIGatewayProxyEvent
): Promise<APIGatewayProxyResult> => {
  try {
    const auth = requireAuth(event);

    const route = `${event.httpMethod} ${event.resource}`;
    switch (route) {
      case "GET /vault/entries":
        return await listEntries(event, auth);
      case "POST /vault/entries":
        return await createEntry(event, auth);
      case "GET /vault/entries/{id}":
        return await getEntry(event, auth);
      case "PUT /vault/entries/{id}":
        return await updateEntry(event, auth);
      case "DELETE /vault/entries/{id}":
        return await deleteEntry(event, auth);
      case "POST /vault/entries/{id}/upload":
        return await uploadEntry(event, auth);
      case "POST /vault/entries/{id}/upload/complete":
        return await completeUploadEntry(event, auth);
      case "GET /vault/entries/{id}/download":
        return await downloadEntry(event, auth);
      case "POST /vault/search":
        return await searchEntries(event, auth);
      default:
        return handleError(new Error(`Unsupported route: ${route}`));
    }
  } catch (err) {
    return handleError(err);
  }
};
