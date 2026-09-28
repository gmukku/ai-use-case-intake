"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import ReviewerBar from "../components/ReviewerBar";
import RiskBadge from "../components/RiskBadge";
import { type QueueItem, getQueue } from "@/lib/review";

const STATUS_LABEL: Record<string, string> = {
  pending: "Needs review",
  approved: "Approved",
  rejected: "Rejected",
  sent_back: "Sent back",
};

export default function Queue() {
  const [items, setItems] = useState<QueueItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getQueue()
      .then(setItems)
      .catch((failure) => setError(failure.message ?? "Could not load the queue."));
  }, []);

  // Waiting on a human first, then everything else; the queue is a worklist, not an archive.
  const sorted = items
    ? [...items].sort(
        (a, b) => Number(b.review_status === "pending") - Number(a.review_status === "pending"),
      )
    : null;

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col gap-6 px-5 py-8">
      <header className="border-b border-line pb-6">
        <h1 className="text-xl font-semibold tracking-tight">Review queue</h1>
        <p className="mt-1.5 text-sm text-muted">
          Completed discovery conversations, waiting on a human before anything gets built.
        </p>
      </header>

      <ReviewerBar />

      {error && (
        <p
          role="alert"
          className="rounded-xl border border-red-300/60 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
        >
          {error}
        </p>
      )}

      {sorted === null && !error && <p className="text-sm text-muted">Loading…</p>}

      {sorted?.length === 0 && (
        <p className="text-sm text-muted">
          Nothing here yet. A conversation appears once all seven canvas categories are covered.
        </p>
      )}

      <ul className="flex flex-col gap-2">
        {sorted?.map((item) => (
          <li key={item.session_id}>
            <Link
              href={`/review/${item.session_id}`}
              className="flex flex-col gap-2 rounded-xl border border-line bg-surface px-4 py-3.5 transition-colors hover:border-muted"
            >
              <div className="flex items-start justify-between gap-4">
                <span className="text-[15px] leading-snug font-medium">{item.title}</span>
                <RiskBadge level={item.risk_level} scrutiny={item.needs_extra_scrutiny} />
              </div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
                <span
                  className={
                    item.review_status === "pending" ? "font-medium text-foreground" : undefined
                  }
                >
                  {STATUS_LABEL[item.review_status] ?? item.review_status}
                </span>
                <span>·</span>
                <span>{item.turns} turns</span>
                {!item.ready_for_review && (
                  <>
                    <span>·</span>
                    <span title="Some categories still have open gaps.">has open gaps</span>
                  </>
                )}
                {item.live && (
                  <>
                    <span>·</span>
                    <span>conversation still open</span>
                  </>
                )}
                <span>·</span>
                <span className="font-mono">{item.session_id.slice(0, 8)}</span>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
