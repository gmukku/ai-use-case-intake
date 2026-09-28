import { clearReviewer, currentReviewer, setReviewer } from "@/lib/server/reviewer";

/** Who the browser is currently signing decisions as. */
export async function GET(): Promise<Response> {
  return Response.json({ reviewer: await currentReviewer() });
}

/** Set the name that will be recorded on decisions from this browser. */
export async function POST(request: Request): Promise<Response> {
  let body: { name?: unknown };
  try {
    body = await request.json();
  } catch {
    return Response.json({ detail: "Malformed request." }, { status: 400 });
  }
  const name = typeof body.name === "string" ? body.name.trim() : "";
  if (!name) {
    return Response.json({ detail: "A name is required." }, { status: 422 });
  }
  await setReviewer(name);
  return Response.json({ reviewer: name });
}

export async function DELETE(): Promise<Response> {
  await clearReviewer();
  return new Response(null, { status: 204 });
}
