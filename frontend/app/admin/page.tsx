"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import EvalsPanel from "../components/EvalsPanel";
import { type AdminSession, getSessions } from "@/lib/admin";

const BUILD_MARK: Record<AdminSession["build_status"], string> = {
  none: "",
  running: "building…",
  ok: "built",
  failed: "build failed",
};

export default function Admin() {
  const [sessions, setSessions] = useState<AdminSession[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const rows = await getSessions();
        if (!cancelled) setSessions(rows);
      } catch (failure) {
        if (!cancelled) {
          setError(failure instanceof Error ? failure.message : "Could not load sessions.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const spend = sessions?.reduce((total, s) => total + s.total_cost_usd, 0) ?? 0;

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-8 px-5 py-8">
      <header className="border-b border-line pb-6">
        <h1 className="text-xl font-semibold tracking-tight">Admin</h1>
        <p className="mt-1.5 text-sm text-muted">
          Every session including the unfinished ones, the full trace behind each, and what the
          evals say. This surface is not for whoever submitted the request.
        </p>
      </header>

      {error && (
        <p
          role="alert"
          className="rounded-xl border border-red-300/60 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
        >
          {error}
        </p>
      )}

      <section className="flex flex-col gap-3">
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-sm font-medium">Sessions</h2>
          {sessions && (
            <span className="text-xs text-muted">
              {sessions.length} total · ${spend.toFixed(4)} spent
            </span>
          )}
        </div>

        {sessions === null && !error && <p className="text-sm text-muted">Loading…</p>}
        {sessions?.length === 0 && <p className="text-sm text-muted">No sessions yet.</p>}

        <ul className="flex flex-col gap-2">
          {sessions?.map((session) => (
            <li key={session.session_id}>
              <Link
                href={`/admin/${session.session_id}`}
                className="flex flex-col gap-2 rounded-xl border border-line bg-surface px-4 py-3 transition-colors hover:border-muted"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-mono text-sm">{session.session_id.slice(0, 8)}</span>
                  <span className="text-xs text-muted">
                    ${session.total_cost_usd.toFixed(4)} · {session.turn} turns
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-1.5 text-xs">
                  <Tag tone={session.is_complete ? "good" : "plain"}>
                    {session.is_complete ? "canvas complete" : `${session.turn} turns in`}
                  </Tag>
                  {session.ready_for_review && <Tag tone="good">ready</Tag>}
                  {Object.keys(session.open_gaps).length > 0 && (
                    <Tag tone="warn">{Object.keys(session.open_gaps).length} open gaps</Tag>
                  )}
                  {session.review_status !== "pending" && <Tag>{session.review_status}</Tag>}
                  {session.build_status !== "none" && (
                    <Tag tone={session.build_status === "failed" ? "bad" : "good"}>
                      {BUILD_MARK[session.build_status]}
                    </Tag>
                  )}
                  {session.departments.map((d) => (
                    <Tag key={d}>{d}</Tag>
                  ))}
                  {session.resumed && <Tag>resumed</Tag>}
                  {session.live && <Tag tone="good">live</Tag>}
                  {session.failure && <Tag tone="bad">failed</Tag>}
                  {Object.entries(session.feedback).map(([turn, rating]) => (
                    <Tag key={turn} tone={rating === "up" ? "good" : "warn"}>
                      {rating === "up" ? "👍" : "👎"} t{turn}
                    </Tag>
                  ))}
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </section>

      <EvalsPanel />
    </main>
  );
}

function Tag({
  children,
  tone = "plain",
}: {
  children: React.ReactNode;
  tone?: "plain" | "good" | "warn" | "bad";
}) {
  const styles = {
    plain: "border-line text-muted",
    good: "border-accent/40 text-accent",
    warn: "border-amber-500/40 text-amber-700 dark:text-amber-300",
    bad: "border-red-500/40 text-red-700 dark:text-red-300",
  }[tone];
  return <span className={`rounded-full border px-2 py-0.5 ${styles}`}>{children}</span>;
}
