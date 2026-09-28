import { relay } from "@/lib/server/backend";

/** The state of a session's build. */
export async function GET(_: Request, { params }: RouteContext<"/api/builds/[id]">) {
  const { id } = await params;
  return relay(`/builds/${encodeURIComponent(id)}`);
}

/**
 * Start the Builder. `force=true` redoes a finished build, which costs money and overwrites
 * the workspace — so the flag has to be asked for, exactly as on the API.
 */
export async function POST(request: Request, { params }: RouteContext<"/api/builds/[id]">) {
  const { id } = await params;
  const force = new URL(request.url).searchParams.get("force") === "true";
  return relay(`/builds/${encodeURIComponent(id)}?force=${force}`, { method: "POST" });
}
