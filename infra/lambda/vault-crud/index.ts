import type { APIGatewayProxyEvent, APIGatewayProxyResult } from "aws-lambda";
import { handleError } from "../shared/response.js";
import { requireAuth } from "../shared/auth.js";
import { ensureUser } from "../shared/db.js";
import { listEntries } from "./list.js";
import { createEntry } from "./create.js";
import { getEntry } from "./get.js";
import { updateEntry } from "./update.js";
import { deleteEntry } from "./delete.js";

export const handler = async (
  event: APIGatewayProxyEvent
): Promise<APIGatewayProxyResult> => {
  try {
    const auth = requireAuth(event);
    await ensureUser(auth.user_id, auth.email);

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
      default:
        return handleError(new Error(`Unsupported route: ${route}`));
    }
  } catch (err) {
    return handleError(err);
  }
};
