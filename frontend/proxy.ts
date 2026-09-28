import { type NextRequest, NextResponse } from "next/server";

import { SESSION_COOKIE, allows, isConfigured, readSession } from "@/lib/server/auth";

/**
 * The gate in front of the reviewer and admin surfaces.
 *
 * One place decides, rather than a check at the top of every route handler — the same reason
 * `test_isolation.py` scans for `env=AGENT_ENV` instead of trusting each call site. A new
 * route under `/admin` or `/api/admin` is protected by existing here, not by remembering.
 *
 * The requester surface (`/`, `/s/<id>`, and the `/sessions` calls it makes straight to the
 * API) stays open: a stakeholder has no account and should not need one.
 *
 * In Next 16 this file is `proxy.ts`, not `middleware.ts`, and runs on the Node runtime —
 * which is what lets it verify an HMAC.
 */

/** Paths that need a signed-in reviewer, and the ones that need an admin on top. */
const ADMIN_PREFIXES = ["/admin", "/api/admin", "/api/builds"];
const REVIEWER_PREFIXES = ["/review", "/api/reviews"];

function requiredRole(pathname: string): "reviewer" | "admin" | null {
  if (ADMIN_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`))) return "admin";
  if (REVIEWER_PREFIXES.some((p) => pathname === p || pathname.startsWith(`${p}/`))) {
    return "reviewer";
  }
  return null;
}

function isApi(pathname: string): boolean {
  return pathname.startsWith("/api/");
}

export function proxy(request: NextRequest): NextResponse {
  const { pathname, search } = request.nextUrl;
  const required = requiredRole(pathname);
  if (!required) return NextResponse.next();

  // Fail closed. No session secret or no users configured means the door is shut, not open.
  if (!isConfigured()) {
    return isApi(pathname)
      ? NextResponse.json(
          { detail: "Sign-in is not configured on this server. See frontend/README.md." },
          { status: 503 },
        )
      : NextResponse.redirect(new URL("/login?error=unconfigured", request.url));
  }

  const session = readSession(request.cookies.get(SESSION_COOKIE)?.value);
  if (!session) {
    if (isApi(pathname)) {
      return NextResponse.json({ detail: "Not signed in." }, { status: 401 });
    }
    const login = new URL("/login", request.url);
    // Come back to where they were aiming once they are in.
    login.searchParams.set("next", `${pathname}${search}`);
    return NextResponse.redirect(login);
  }

  if (!allows(session.role, required)) {
    return isApi(pathname)
      ? NextResponse.json({ detail: "This needs an admin account." }, { status: 403 })
      : NextResponse.redirect(new URL("/review?error=forbidden", request.url));
  }

  return NextResponse.next();
}

export const config = {
  // Only the protected trees. Without a matcher this would also run on every static asset.
  matcher: ["/admin/:path*", "/review/:path*", "/api/admin/:path*", "/api/reviews/:path*", "/api/builds/:path*"],
};
