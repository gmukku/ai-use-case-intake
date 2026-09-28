import { relay } from "@/lib/server/backend";
import { currentReviewer } from "@/lib/server/reviewer";

/**
 * Record a decision.
 *
 * The reviewer's name comes from the httpOnly cookie, never from the request body: the page
 * says *what* was decided, the server says *who* decided it. Anything the client sends under
 * `reviewer` is discarded.
 */
export async function POST(request: Request, { params }: RouteContext<"/api/reviews/[id]/decision">) {
  const { id } = await params;
  const reviewer = await currentReviewer();
  if (!reviewer) {
    return Response.json({ detail: "Add your name before deciding." }, { status: 401 });
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
      reviewer,
    }),
  });
}
