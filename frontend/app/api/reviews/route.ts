import { relay } from "@/lib/server/backend";

/** The approval queue. */
export async function GET(): Promise<Response> {
  return relay("/reviews");
}
