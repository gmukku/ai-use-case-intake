import "server-only";

/**
 * The only place the admin token exists.
 *
 * The reviewer and admin routes on FastAPI require a bearer token. A token in browser
 * JavaScript is a token anyone can read out of the bundle, so the browser never gets one:
 * it talks same-origin to this app's route handlers, and those run on the server and add
 * the header on the way through.
 *
 * `import "server-only"` makes that structural rather than a habit — importing this module
 * from a Client Component fails the build instead of quietly shipping the token.
 */

// Server-side only: no NEXT_PUBLIC_ prefix, so it is never inlined into the client bundle.
const BASE = process.env.BLUEPRINT_API_URL ?? "http://127.0.0.1:8000";
const TOKEN = process.env.BLUEPRINT_ADMIN_TOKEN;

export async function backend(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set("Content-Type", "application/json");
  if (TOKEN) headers.set("Authorization", `Bearer ${TOKEN}`);
  return fetch(`${BASE}${path}`, { ...init, headers, cache: "no-store" });
}

/**
 * Forward a request to the backend and relay the answer.
 *
 * Status and body are passed through so the browser sees the API's own errors — a 409 on an
 * incomplete canvas should read as a 409, not as a generic failure. A backend that is simply
 * down becomes a 502, because that is what it is from the browser's side.
 */
export async function relay(path: string, init?: RequestInit): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await backend(path, init);
  } catch {
    return Response.json({ detail: "The API is not reachable." }, { status: 502 });
  }
  const body = await upstream.text();
  return new Response(body || null, {
    status: upstream.status,
    headers: { "Content-Type": upstream.headers.get("Content-Type") ?? "application/json" },
  });
}
