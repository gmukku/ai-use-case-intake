import { relay } from "@/lib/server/backend";

/**
 * One compiled spec, with its risk assessment.
 *
 * `summarize=true` asks the API for a model-written title and narrative instead of the first
 * eighty characters of the opener. It costs about a cent, so it is the reviewer's choice
 * rather than something the queue does for every row.
 */
export async function GET(request: Request, { params }: RouteContext<"/api/reviews/[id]">) {
  const { id } = await params;
  const summarize = new URL(request.url).searchParams.get("summarize") === "true";
  return relay(`/reviews/${encodeURIComponent(id)}?summarize=${summarize}`);
}
