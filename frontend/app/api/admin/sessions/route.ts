import { relay } from "@/lib/server/backend";

/** Every session, including the ones the review queue filters out. */
export async function GET(): Promise<Response> {
  return relay("/admin/sessions");
}
