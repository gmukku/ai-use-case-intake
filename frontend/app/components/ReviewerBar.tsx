"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

type Session = { username: string; name: string; role: "reviewer" | "admin" };

/**
 * Who decisions are recorded as.
 *
 * Until step 12 this was a name the reviewer typed into a cookie — non-forgeable from the
 * page, but anyone could claim to be anyone. It is now the authenticated account, so the name
 * in the audit log is one somebody signed in to use.
 */
export default function ReviewerBar({ onChange }: { onChange?: (name: string | null) => void }) {
  const router = useRouter();
  const [session, setSession] = useState<Session | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const response = await fetch("/api/auth");
        const body = await response.json();
        if (!cancelled) {
          setSession(body.session);
          onChange?.(body.session?.name ?? null);
        }
      } catch {
        if (!cancelled) setSession(null);
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
    // onChange is a new closure each render; depending on it would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function signOut() {
    await fetch("/api/auth", { method: "DELETE" }).catch(() => {});
    router.replace("/login");
    router.refresh();
  }

  if (!loaded || !session) return null;

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-surface px-4 py-3 text-sm">
      <p>
        Signed in as <span className="font-medium">{session.name}</span>
        <span className="text-muted"> · {session.role}</span>
      </p>
      <button
        type="button"
        onClick={signOut}
        className="text-xs text-muted underline-offset-2 hover:underline"
      >
        Sign out
      </button>
    </div>
  );
}
