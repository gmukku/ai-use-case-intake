import { relay } from "@/lib/server/backend";

/** The whole snapshot for one session: canvas, every turn, verdicts, tool calls, decisions. */
export async function GET(_: Request, { params }: RouteContext<"/api/admin/sessions/[id]/trace">) {
  const { id } = await params;
  return relay(`/admin/sessions/${encodeURIComponent(id)}/trace`);
}
