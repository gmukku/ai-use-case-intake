"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import {
  type BuildStatus,
  type Trace,
  type Turn,
  getBuild,
  getTrace,
  startBuild,
} from "@/lib/admin";

/** Short names for the MCP tools, since `mcp__canvas__record_canvas_answer` is mostly prefix. */
function toolLabel(name: string): string {
  return name.replace(/^mcp__/, "").replace(/__/g, " · ");
}

export default function TraceView({ sessionId }: { sessionId: string }) {
  const [trace, setTrace] = useState<Trace | null>(null);
  const [build, setBuild] = useState<BuildStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const [fresh, buildState] = await Promise.all([getTrace(sessionId), getBuild(sessionId)]);
    setTrace(fresh);
    setBuild(buildState);
  }, [sessionId]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [fresh, buildState] = await Promise.all([getTrace(sessionId), getBuild(sessionId)]);
        if (!cancelled) {
          setTrace(fresh);
          setBuild(buildState);
        }
      } catch (failure) {
        if (!cancelled) {
          setError(failure instanceof Error ? failure.message : "Could not load the trace.");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // A build runs in the background on the API, so the only way to learn it finished is to ask.
  useEffect(() => {
    if (build?.status !== "running") return;
    const timer = setInterval(() => {
      getBuild(sessionId)
        .then(setBuild)
        .catch(() => {});
    }, 4000);
    return () => clearInterval(timer);
  }, [build?.status, sessionId]);

  async function run(force: boolean) {
    setBusy(true);
    setError(null);
    try {
      setBuild(await startBuild(sessionId, force));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Could not start the build.");
    } finally {
      setBusy(false);
    }
  }

  const approved = trace?.review_status === "approved";
  const finished = build && (build.status === "ok" || build.status === "failed");

  return (
    <main className="mx-auto flex w-full max-w-4xl flex-1 flex-col gap-6 px-5 py-8">
      <Link href="/admin" className="text-sm text-muted underline-offset-2 hover:underline">
        ← Sessions
      </Link>

      {error && (
        <p
          role="alert"
          className="rounded-xl border border-red-300/60 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
        >
          {error}
        </p>
      )}

      {!trace && !error && <p className="text-sm text-muted">Loading…</p>}

      {trace && (
        <>
          <header className="flex flex-col gap-2 border-b border-line pb-6">
            <h1 className="font-mono text-lg font-semibold">{trace.session_id}</h1>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
              <span>{trace.model}</span>
              <span>·</span>
              <span>{trace.turn} turns</span>
              <span>·</span>
              <span>${trace.total_cost_usd.toFixed(4)}</span>
              <span>·</span>
              <span>{trace.review_status}</span>
              {trace.resumed && (
                <>
                  <span>·</span>
                  <span>resumed</span>
                </>
              )}
            </div>
            {trace.failure && (
              <p className="rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm">
                {trace.failure}
              </p>
            )}
          </header>

          {trace.match && (
            <Section title="Classification">
              <p className="text-sm">
                <span className="font-medium">{trace.match.departments.join(", ")}</span>
                <span className="text-muted">
                  {" "}
                  · {trace.match.model}
                  {trace.match.cost_usd !== null && ` · $${trace.match.cost_usd.toFixed(4)}`}
                </span>
              </p>
              <p className="mt-1.5 text-sm text-muted">{trace.match.rationale}</p>
            </Section>
          )}

          {trace.web_search && trace.web_search.attempts.length > 0 && (
            <Section title={`Web search (cap ${trace.web_search.max_calls})`}>
              <ul className="flex flex-col gap-1 text-sm">
                {trace.web_search.attempts.map((attempt, i) => (
                  <li key={i} className={attempt.allowed ? "" : "text-muted line-through"}>
                    {attempt.query}
                    {!attempt.allowed && " — denied by the hook"}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title="Canvas">
            <div className="flex flex-col gap-2">
              {Object.entries(trace.canvas.current).map(([category, summary]) => {
                const versions = trace.canvas.entries.filter((e) => e.category === category).length;
                const gaps = trace.open_gaps[category];
                return (
                  <div key={category} className="border-l-2 border-line pl-3">
                    <div className="flex items-baseline gap-2">
                      <span className="font-mono text-xs">{category}</span>
                      {versions > 0 && <span className="text-xs text-muted">v{versions}</span>}
                      {gaps?.length > 0 && (
                        <span className="text-xs text-amber-700 dark:text-amber-300">
                          open: {gaps.join(", ")}
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 text-sm">
                      {summary ?? <span className="text-muted">not captured</span>}
                    </p>
                  </div>
                );
              })}
            </div>
          </Section>

          <Section title={`Turns (${trace.turns.length} stored)`}>
            <div className="flex flex-col gap-3">
              {trace.turns.map((turn, i) => (
                <TurnCard key={i} turn={turn} />
              ))}
            </div>
          </Section>

          {trace.feedback.length > 0 && (
            <Section title="Requester feedback">
              <ul className="flex flex-col gap-1 text-sm">
                {trace.feedback.map((entry, i) => (
                  <li key={i}>
                    {entry.rating === "up" ? "👍" : "👎"} turn {entry.turn}
                    {entry.comment && <span className="text-muted"> — {entry.comment}</span>}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title="Build">
            {!approved && (
              <p className="text-sm text-muted">
                Not approved. The Builder only runs behind the review gate.
              </p>
            )}
            {approved && (
              <div className="flex flex-col gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => run(false)}
                    disabled={busy || build?.status === "running" || !!finished}
                    className="rounded-xl bg-accent px-4 py-2 text-sm font-medium text-background disabled:opacity-40"
                  >
                    {build?.status === "running" ? "Building…" : "Run the Builder"}
                  </button>
                  {finished && (
                    <button
                      type="button"
                      onClick={() => run(true)}
                      disabled={busy}
                      title="Rebuilds and overwrites the workspace. Costs about $0.46."
                      className="rounded-xl border border-line px-4 py-2 text-sm disabled:opacity-40"
                    >
                      Rebuild (force)
                    </button>
                  )}
                  <span className="text-xs text-muted">
                    ~$0.46 and a couple of minutes per build.
                  </span>
                </div>
                {build?.result && <BuildReport result={build.result} />}
              </div>
            )}
          </Section>

          {trace.reviews.length > 0 && (
            <Section title="Decisions">
              <ul className="flex flex-col gap-2 text-sm">
                {trace.reviews.map((decision, i) => (
                  <li key={i}>
                    <span className="font-medium">{decision.action.replace(/_/g, " ")}</span>
                    <span className="text-muted">
                      {" "}
                      by {decision.reviewer} · {new Date(decision.decided_at).toLocaleString()}
                    </span>
                    {decision.note && <p className="text-muted">{decision.note}</p>}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <button
            type="button"
            onClick={() => refresh().catch(() => setError("Refresh failed."))}
            className="self-start text-sm text-muted underline-offset-2 hover:underline"
          >
            Refresh
          </button>
        </>
      )}
    </main>
  );
}

function TurnCard({ turn }: { turn: Turn }) {
  return (
    <div className="rounded-xl border border-line bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span className="font-medium text-foreground">turn {turn.turn}</span>
        <span>{turn.origin}</span>
        {turn.cost_usd !== null && <span>${turn.cost_usd.toFixed(4)}</span>}
        <span>{(turn.duration_ms / 1000).toFixed(1)}s</span>
        {turn.num_agentic_turns !== null && <span>{turn.num_agentic_turns} agentic</span>}
        {turn.is_error && <span className="text-red-600 dark:text-red-400">error</span>}
      </div>

      {turn.user_text && (
        <p className="mt-2 border-l-2 border-line pl-3 text-sm whitespace-pre-wrap">
          {turn.user_text}
        </p>
      )}

      {turn.tool_calls.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1">
          {turn.tool_calls.map((call) => (
            <li key={call.id} className="font-mono text-xs text-muted">
              → {toolLabel(call.name)}
              {Object.keys(call.input).length > 0 && (
                <span> {JSON.stringify(call.input).slice(0, 160)}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {turn.assessments.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1 text-xs">
          {turn.assessments.map((assessment, i) => (
            <li key={i} className={assessment.sufficient ? "text-muted" : "text-amber-600"}>
              {assessment.sufficient ? "✓" : "✗"} {assessment.category} v{assessment.version}
              {assessment.missing.length > 0 && ` — missing ${assessment.missing.join(", ")}`}
              {assessment.question && (
                <span className="text-muted"> → “{assessment.question}”</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {turn.assistant_text && (
        <p className="mt-2 text-sm whitespace-pre-wrap">{turn.assistant_text}</p>
      )}

      {turn.errors.length > 0 && (
        <p className="mt-2 text-sm text-red-600 dark:text-red-400">{turn.errors.join("; ")}</p>
      )}
    </div>
  );
}

function BuildReport({ result }: { result: NonNullable<BuildStatus["result"]> }) {
  if (result.error) {
    return (
      <p className="rounded-xl border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm">
        {result.error}
      </p>
    );
  }
  return (
    <div className="rounded-xl border border-line bg-surface px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span className={result.ok ? "text-accent" : "text-red-600 dark:text-red-400"}>
          {result.ok ? "verified" : "verification failed"}
        </span>
        <span>{result.template}</span>
        <span>{result.verification.files.length} files</span>
        <span>{result.verification.total_lines} lines</span>
        {result.cost_usd !== null && <span>${result.cost_usd.toFixed(4)}</span>}
        <span>{(result.duration_ms / 1000).toFixed(0)}s</span>
      </div>
      <p className="mt-2 font-mono text-xs text-muted">
        {result.workspace} · {result.verification.files.join(", ")}
      </p>
      {result.verification.violations.length > 0 && (
        <p className="mt-2 text-sm text-amber-700 dark:text-amber-300">
          {result.verification.violations.join("; ")}
        </p>
      )}
      {result.report && <p className="mt-2 text-sm whitespace-pre-wrap">{result.report}</p>}
      {result.verification.test_output && (
        <pre className="mt-2 overflow-x-auto rounded-lg bg-background px-3 py-2 font-mono text-xs text-muted">
          {result.verification.test_output.slice(-1200)}
        </pre>
      )}
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-sm font-medium">{title}</h2>
      <div className="rounded-xl border border-line bg-surface px-4 py-3">{children}</div>
    </section>
  );
}
