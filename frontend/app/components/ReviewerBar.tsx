"use client";

import { useEffect, useState } from "react";

import { getReviewer, signIn } from "@/lib/review";

/**
 * Who decisions get recorded as.
 *
 * The name is held in an httpOnly cookie the server reads when a decision is posted, so it
 * cannot be changed per-request by the page. It is a signature, not a login — the banner
 * says so rather than implying the queue is protected.
 */
export default function ReviewerBar({ onChange }: { onChange?: (name: string | null) => void }) {
  const [reviewer, setReviewer] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    getReviewer()
      .then(({ reviewer: name }) => {
        setReviewer(name);
        onChange?.(name);
      })
      .catch(() => setReviewer(null))
      .finally(() => setLoaded(true));
    // onChange is a fresh closure each render; re-running on it would loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit() {
    const name = draft.trim();
    if (!name) return;
    try {
      const { reviewer: saved } = await signIn(name);
      setReviewer(saved);
      setDraft("");
      onChange?.(saved);
    } catch {
      // The decision call will say so more usefully than a banner would.
    }
  }

  if (!loaded) return null;

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-line bg-surface px-4 py-3">
      {reviewer ? (
        <p className="text-sm">
          Signing decisions as <span className="font-medium">{reviewer}</span>
          <button
            type="button"
            onClick={() => {
              setReviewer(null);
              onChange?.(null);
            }}
            className="ml-3 text-xs text-muted underline-offset-2 hover:underline"
          >
            change
          </button>
        </p>
      ) : (
        <div className="flex flex-1 items-center gap-2">
          <label htmlFor="reviewer-name" className="text-sm text-muted">
            Your name
          </label>
          <input
            id="reviewer-name"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") submit();
            }}
            placeholder="so decisions are attributable"
            className="min-w-48 flex-1 rounded-lg border border-line bg-background px-3 py-1.5 text-sm outline-none placeholder:text-muted"
          />
          <button
            type="button"
            onClick={submit}
            disabled={!draft.trim()}
            className="rounded-lg bg-accent px-3 py-1.5 text-sm font-medium text-background disabled:opacity-40"
          >
            Save
          </button>
        </div>
      )}
      <p className="text-xs text-muted">Not a login — put SSO in front of this in deployment.</p>
    </div>
  );
}
