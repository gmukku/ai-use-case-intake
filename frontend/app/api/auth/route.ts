import { cookies } from "next/headers";

import {
  SESSION_COOKIE,
  SESSION_MAX_AGE_S,
  authenticate,
  isConfigured,
  issueSession,
  readSession,
} from "@/lib/server/auth";
import { checkLogin, clientIp, recordFailure, recordSuccess } from "@/lib/server/rate-limit";

/** Who is signed in, if anyone. Used by the UI to show a name and a sign-out link. */
export async function GET(): Promise<Response> {
  const store = await cookies();
  const session = readSession(store.get(SESSION_COOKIE)?.value);
  return Response.json({ session, configured: isConfigured() });
}

/** Sign in. */
export async function POST(request: Request): Promise<Response> {
  if (!isConfigured()) {
    return Response.json(
      { detail: "Sign-in is not configured on this server. See frontend/README.md." },
      { status: 503 },
    );
  }

  let body: { username?: unknown; password?: unknown };
  try {
    body = await request.json();
  } catch {
    return Response.json({ detail: "Malformed request." }, { status: 400 });
  }

  const username = typeof body.username === "string" ? body.username.trim() : "";
  const password = typeof body.password === "string" ? body.password : "";
  if (!username || !password) {
    return Response.json({ detail: "Username and password are required." }, { status: 422 });
  }

  // Before any password work, so a throttled attacker cannot keep spending scrypt time. The
  // limit is on attempts, not on accounts: nothing here has looked in the user store yet.
  const ip = clientIp(request);
  const budget = checkLogin(username, ip);
  if (!budget.allowed) {
    return Response.json(
      { detail: "Too many sign-in attempts. Try again shortly." },
      { status: 429, headers: { "Retry-After": String(budget.retryAfterS) } },
    );
  }

  const session = await authenticate(username, password);
  if (!session) {
    recordFailure(username, ip);
    // One message for both failures: saying which was wrong tells an attacker which usernames
    // exist. `authenticate` also spends the same time either way.
    return Response.json({ detail: "Those details did not match." }, { status: 401 });
  }
  recordSuccess(username);

  const token = issueSession(session);
  if (!token) {
    return Response.json({ detail: "Sign-in is not configured." }, { status: 503 });
  }

  const store = await cookies();
  store.set(SESSION_COOKIE, token, {
    httpOnly: true, // not readable by document.cookie
    sameSite: "lax", // survives following a link in, blocks cross-site form posts
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: SESSION_MAX_AGE_S,
  });
  return Response.json({ session });
}

/** Sign out. */
export async function DELETE(): Promise<Response> {
  const store = await cookies();
  store.delete(SESSION_COOKIE);
  return new Response(null, { status: 204 });
}
