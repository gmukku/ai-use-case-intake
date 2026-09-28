import { cookies } from "next/headers";

import { SESSION_COOKIE, readSession } from "@/lib/server/auth";
import { relay } from "@/lib/server/backend";

/**
 * Record a decision.
 *
 * The reviewer's name comes from the signed session, never from the request body: the page
 * says *what* was decided, the server says *who* decided it, and "who" is now an account
 * somebody signed in to use rather than a name they typed. Anything the client sends under
 * `reviewer` is discarded.
 *
 * `proxy.ts` has already refused this request without a valid session; the check here is the
 * second lock, because a route that assumes the gate is in front of it breaks quietly the day
 * someone edits the matcher.
 */
export async function POST(
  request: Request,
  { params }: RouteContext<"/api/reviews/[id]/decision">,
) {
  const { id } = await params;
  const store = await cookies();
  const session = readSession(store.get(SESSION_COOKIE)?.value);
  if (!session) {
    return Response.json({ detail: "Not signed in." }, { status: 401 });
  }

  let body: { action?: unknown; note?: unknown };
  try {
    body = await request.json();
  } catch {
    return Response.json({ detail: "Malformed request." }, { status: 400 });
  }

  return relay(`/reviews/${encodeURIComponent(id)}/decision`, {
    method: "POST",
    body: JSON.stringify({
      action: body.action,
      note: typeof body.note === "string" ? body.note : "",
      reviewer: session.name,
    }),
  });
}
