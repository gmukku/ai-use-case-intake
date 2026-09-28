import { relay } from "@/lib/server/backend";

/** One eval run in full, including every per-call result. */
export async function GET(_: Request, { params }: RouteContext<"/api/admin/evals/[suite]/[run]">) {
  const { suite, run } = await params;
  return relay(`/admin/evals/${encodeURIComponent(suite)}/${encodeURIComponent(run)}`);
}
