"use client";

import type { RiskLevel } from "@/lib/review";

const STYLES: Record<RiskLevel, string> = {
  low: "border-line bg-surface text-muted",
  elevated: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300",
  high: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300",
};

export default function RiskBadge({
  level,
  scrutiny = false,
}: {
  level: RiskLevel;
  scrutiny?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={`rounded-full border px-2 py-0.5 text-xs font-medium tracking-wide uppercase ${STYLES[level]}`}
      >
        {level}
      </span>
      {scrutiny && (
        <span
          title="A named system integration is involved; CLAUDE.md asks for extra scrutiny here."
          className="text-xs text-muted"
        >
          ⚑ extra scrutiny
        </span>
      )}
    </span>
  );
}
