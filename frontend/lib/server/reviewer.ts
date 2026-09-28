import "server-only";

import { cookies } from "next/headers";

/**
 * Who the reviewer is.
 *
 * The API takes `reviewer` in the decision body, which means the *client* decides whose name
 * goes in the audit log. Reading it from an httpOnly cookie on the server instead means the
 * page cannot put someone else's name on a decision, even by editing the request.
 *
 * This is not authentication. Anyone who can reach this app can set the cookie and approve
 * things; the cookie only makes the recorded name consistent and non-forgeable *from the
 * page*. The `/review` routes need real SSO in front of them before this is deployed
 * anywhere the queue matters — tracked in docs/LEARNING_LOG.md under "Deferred and open".
 */
export const REVIEWER_COOKIE = "blueprint_reviewer";

const MAX_AGE_S = 60 * 60 * 24 * 30;

export async function currentReviewer(): Promise<string | null> {
  const store = await cookies();
  const value = store.get(REVIEWER_COOKIE)?.value?.trim();
  return value ? value : null;
}

export async function setReviewer(name: string): Promise<void> {
  const store = await cookies();
  store.set(REVIEWER_COOKIE, name.trim().slice(0, 120), {
    httpOnly: true, // not readable by document.cookie
    sameSite: "lax",
    path: "/",
    maxAge: MAX_AGE_S,
  });
}

export async function clearReviewer(): Promise<void> {
  const store = await cookies();
  store.delete(REVIEWER_COOKIE);
}
