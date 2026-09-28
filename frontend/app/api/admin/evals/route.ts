import { relay } from "@/lib/server/backend";

/** Recorded eval runs, grouped by suite. */
export async function GET(): Promise<Response> {
  return relay("/admin/evals");
}
