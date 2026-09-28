"use client";

import { useEffect, useState } from "react";

import { type EvalSuites, getEvals } from "@/lib/admin";

/** Metrics worth a column, in the order they answer "is this model good enough". */
const COLUMNS: { key: string; label: string; format: (v: number) => string }[] = [
  { key: "exact_match", label: "exact", format: (v) => `${(v * 100).toFixed(1)}%` },
  { key: "mean_jaccard", label: "jaccard", format: (v) => v.toFixed(3) },
  { key: "over_selection_rate", label: "over", format: (v) => `${(v * 100).toFixed(1)}%` },
  { key: "under_selection_rate", label: "under", format: (v) => `${(v * 100).toFixed(1)}%` },
  { key: "errors", label: "errors", format: (v) => String(v) },
  { key: "mean_cost_usd", label: "$/call", format: (v) => `$${v.toFixed(4)}` },
  { key: "p95_ms", label: "p95", format: (v) => `${(v / 1000).toFixed(1)}s` },
];

/**
 * What the recorded eval runs say.
 *
 * Read-only on purpose: running a suite costs real money and takes minutes, so it stays a
 * deliberate command-line act rather than something a page can set off by accident.
 */
export default function EvalsPanel() {
  const [data, setData] = useState<EvalSuites | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const suites = await getEvals();
        if (!cancelled) setData(suites);
      } catch {
        if (!cancelled) setError("Could not load eval runs.");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="flex flex-col gap-3">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-sm font-medium">Evals</h2>
        <code className="text-xs text-muted">uv run python evals/&lt;suite&gt;/run.py</code>
      </div>

      {error && <p className="text-sm text-muted">{error}</p>}
      {data === null && !error && <p className="text-sm text-muted">Loading…</p>}
      {data?.suites.length === 0 && (
        <p className="text-sm text-muted">No suites yet — the harness lands in step 11.</p>
      )}

      {data?.suites.map(({ suite, runs }) => (
        <div key={suite} className="rounded-xl border border-line bg-surface px-4 py-3.5">
          <div className="flex items-baseline justify-between gap-3">
            <h3 className="text-sm font-medium">{suite.replace(/_/g, " ")}</h3>
            <span className="text-xs text-muted">
              {runs.length} run{runs.length === 1 ? "" : "s"}
            </span>
          </div>

          {runs.length === 0 ? (
            <p className="mt-2 text-sm text-muted">Never run.</p>
          ) : (
            <Run run={runs[0]} />
          )}
        </div>
      ))}
    </section>
  );
}

function Run({ run }: { run: EvalSuites["suites"][number]["runs"][number] }) {
  const models = Object.keys(run.summary);
  // Only show columns this suite actually reported: a risk-gate suite has no jaccard.
  const columns = COLUMNS.filter((c) => models.some((m) => typeof run.summary[m]?.[c.key] === "number"));

  return (
    <div className="mt-3 flex flex-col gap-2">
      <p className="text-xs text-muted">
        {run.calls} calls
        {run.ran_at && ` · ${new Date(run.ran_at).toLocaleString()}`}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-xs text-muted">
              <th className="py-1 pr-4 font-normal">model</th>
              {columns.map((c) => (
                <th key={c.key} className="py-1 pr-4 font-normal">
                  {c.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {models.map((model) => (
              <tr key={model} className="border-t border-line">
                <td className="py-1.5 pr-4 font-mono text-xs">{model}</td>
                {columns.map((c) => {
                  const value = run.summary[model]?.[c.key];
                  return (
                    <td key={c.key} className="py-1.5 pr-4 tabular-nums">
                      {typeof value === "number" ? c.format(value) : "—"}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
