"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import ReviewerBar from "./ReviewerBar";
import RiskBadge from "./RiskBadge";
import { type Action, type ReviewDetail as Detail, decide, getReview } from "@/lib/review";

const ACTIONS: { action: Action; label: string; hint: string }[] = [
  { action: "approve", label: "Approve", hint: "The Builder may scaffold a prototype." },
  { action: "send_back", label: "Send back", hint: "Ask the requester one more question." },
  { action: "reject", label: "Reject", hint: "Stop here; nothing gets built." },
];

export default function ReviewDetail({ sessionId }: { sessionId: string }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [summarizing, setSummarizing] = useState(false);
  const [reviewer, setReviewer] = useState<string | null>(null);

  const load = useCallback(
    async (summarize: boolean) => {
      try {
        setDetail(await getReview(sessionId, summarize));
        setError(null);
      } catch (failure) {
        setError(failure instanceof Error ? failure.message : "Could not load this spec.");
      }
    },
    [sessionId],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const fresh = await getReview(sessionId, false);
        if (!cancelled) setDetail(fresh);
      } catch (failure) {
        if (!cancelled) {
          setError(failure instanceof Error ? failure.message : "Could not load this spec.");
        }
      }
    })();
    // A late response must not write into a component that has gone away.
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  async function submit(action: Action) {
    // The API rejects a note-less send-back or reject anyway; saying so here avoids a
    // round trip and puts the message next to the box it is about.
    if (action !== "approve" && !note.trim()) {
      setError("A note is required to send back or reject.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await decide(sessionId, action, note.trim());
      setNote("");
      await load(false);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not record that decision.");
    } finally {
      setBusy(false);
    }
  }

  async function summarize() {
    setSummarizing(true);
    await load(true);
    setSummarizing(false);
  }

  const spec = detail?.spec;
  const decided = detail && detail.review_status !== "pending";

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 px-5 py-8">
      <Link href="/review" className="text-sm text-muted underline-offset-2 hover:underline">
        ← Queue
      </Link>

      {error && (
        <p
          role="alert"
          className="rounded-xl border border-red-300/60 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
        >
          {error}
        </p>
      )}

      {!spec && !error && <p className="text-sm text-muted">Loading…</p>}

      {spec && detail && (
        <>
          <header className="flex flex-col gap-3 border-b border-line pb-6">
            <div className="flex items-start justify-between gap-4">
              <h1 className="text-xl leading-snug font-semibold tracking-tight">{spec.title}</h1>
              <RiskBadge level={spec.risk.level} scrutiny={spec.risk.needs_extra_scrutiny} />
            </div>
            <p className="text-[15px] leading-relaxed text-muted">{spec.narrative}</p>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <span>spec v{spec.version}</span>
              <span>·</span>
              <span>{spec.turns} turns</span>
              <span>·</span>
              <span>${spec.total_cost_usd.toFixed(4)}</span>
              <span>·</span>
              <span className="font-mono">{spec.session_id.slice(0, 8)}</span>
              <span>·</span>
              <button
                type="button"
                onClick={summarize}
                disabled={summarizing}
                className="underline-offset-2 hover:underline disabled:opacity-50"
                title="Ask the model for a title and summary instead of the opener's first line (~1¢)."
              >
                {summarizing ? "summarising…" : "rewrite title"}
              </button>
            </div>
          </header>

          {spec.risk.flags.length > 0 && (
            <section className="flex flex-col gap-2">
              <h2 className="text-sm font-medium">Why this was flagged</h2>
              {spec.risk.flags.map((flag) => (
                <div key={flag.key} className="rounded-xl border border-line bg-surface px-4 py-3">
                  <div className="flex items-center gap-2">
                    <RiskBadge level={flag.level} />
                    <span className="text-sm font-medium">{flag.key.replace(/_/g, " ")}</span>
                  </div>
                  <p className="mt-1.5 text-sm text-muted">{flag.note}</p>
                  {flag.evidence && (
                    <p className="mt-1.5 border-l-2 border-line pl-3 text-sm italic">
                      “{flag.evidence}”
                    </p>
                  )}
                </div>
              ))}
            </section>
          )}

          <section className="flex flex-col gap-3">
            <h2 className="text-sm font-medium">The canvas</h2>
            {spec.categories.map((category) => (
              <div
                key={category.category}
                className="rounded-xl border border-line bg-surface px-4 py-3"
              >
                <div className="flex items-center justify-between gap-3">
                  <h3 className="text-sm font-medium">{category.label}</h3>
                  <span className="text-xs text-muted">
                    v{category.version}
                    {!category.sufficient && " · gaps"}
                  </span>
                </div>
                <p className="mt-1.5 text-[15px] leading-relaxed">{category.summary}</p>
                {category.missing.length > 0 && (
                  <p className="mt-2 text-xs text-muted">
                    Still open: {category.missing.join(", ").replace(/_/g, " ")}
                  </p>
                )}
              </div>
            ))}
          </section>

          <section className="flex flex-col gap-2 text-sm">
            <h2 className="text-sm font-medium">Grounding</h2>
            <dl className="flex flex-col gap-1.5 rounded-xl border border-line bg-surface px-4 py-3 text-muted">
              <Row label="Departments" value={spec.departments.join(", ") || "none matched"} />
              {spec.department_rationale && (
                <Row label="Why" value={spec.department_rationale} />
              )}
              <Row label="SOPs read" value={spec.sops_consulted.join(", ") || "none"} />
              <Row label="Web sources" value={spec.web_sources.join(", ") || "none"} />
            </dl>
          </section>

          {detail.reviews.length > 0 && (
            <section className="flex flex-col gap-2">
              <h2 className="text-sm font-medium">Decisions so far</h2>
              {detail.reviews.map((decision, i) => (
                <div key={i} className="rounded-xl border border-line bg-surface px-4 py-3 text-sm">
                  <span className="font-medium">{decision.action.replace(/_/g, " ")}</span>
                  <span className="text-muted">
                    {" "}
                    by {decision.reviewer} · spec v{decision.spec_version} ·{" "}
                    {new Date(decision.decided_at).toLocaleString()}
                  </span>
                  {decision.note && <p className="mt-1.5 text-muted">{decision.note}</p>}
                </div>
              ))}
            </section>
          )}

          <section className="flex flex-col gap-3 border-t border-line pt-6">
            <ReviewerBar onChange={setReviewer} />

            {decided && (
              <p className="text-sm text-muted">
                Already {detail.review_status.replace(/_/g, " ")}. Deciding again appends to the
                log; it does not replace what is there.
              </p>
            )}

            <textarea
              value={note}
              onChange={(event) => setNote(event.target.value)}
              rows={3}
              placeholder="Note — required for send back and reject, optional for approve"
              aria-label="Decision note"
              className="resize-none rounded-xl border border-line bg-surface px-4 py-3 text-sm outline-none placeholder:text-muted"
            />

            <div className="flex flex-wrap gap-2">
              {ACTIONS.map(({ action, label, hint }) => (
                <button
                  key={action}
                  type="button"
                  title={reviewer ? hint : "Add your name first."}
                  onClick={() => submit(action)}
                  disabled={busy || !reviewer}
                  className={`rounded-xl px-4 py-2 text-sm font-medium transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40 ${
                    action === "approve"
                      ? "bg-accent text-background"
                      : "border border-line bg-surface"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          </section>
        </>
      )}
    </main>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2">
      <dt className="min-w-28 shrink-0 text-foreground">{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
