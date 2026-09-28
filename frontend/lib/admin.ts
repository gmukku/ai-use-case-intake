/**
 * The admin surface, from the browser's side.
 *
 * Same shape as `lib/review.ts`: every path is same-origin, and the route handlers behind
 * them hold the token. Nothing in this file knows the backend's address.
 */

import { ApiError } from "./api";

export type AdminSession = {
  session_id: string;
  live: boolean;
  turn: number;
  is_complete: boolean;
  ready_for_review: boolean;
  review_status: string;
  build_status: "none" | "running" | "ok" | "failed";
  departments: string[];
  open_gaps: Record<string, string[]>;
  total_cost_usd: number;
  model: string | null;
  failure: string | null;
  feedback: Record<string, "up" | "down">;
  resumed: boolean;
};

export type ToolCall = { id: string; name: string; input: Record<string, unknown> };

export type Assessment = {
  category: string;
  version: number;
  sufficient: boolean;
  missing: string[];
  question: string | null;
  model: string;
  cost_usd: number | null;
  duration_ms: number;
};

export type Turn = {
  turn: number;
  user_text: string;
  origin: string;
  assistant_text: string;
  tool_calls: ToolCall[];
  captured: string[];
  assessments: Assessment[];
  cost_usd: number | null;
  session_cost_usd: number | null;
  num_agentic_turns: number | null;
  duration_ms: number;
  is_error: boolean;
  errors: string[];
  started_at: string;
};

export type Trace = {
  session_id: string;
  model: string;
  turn: number;
  total_cost_usd: number;
  is_complete: boolean;
  failure: string | null;
  match: { departments: string[]; rationale: string; model: string; cost_usd: number | null } | null;
  web_search: { max_calls: number; attempts: { query: string; allowed: boolean }[] } | null;
  is_ready_for_review: boolean;
  open_gaps: Record<string, string[]>;
  reviews: { action: string; reviewer: string; note: string; decided_at: string }[];
  review_status: string;
  feedback: { turn: number; rating: string; comment: string; given_at: string }[];
  resumed: boolean;
  canvas: {
    entries: { category: string; summary: string; turn: number; recorded_at: string }[];
    current: Record<string, string | null>;
    missing: string[];
    is_complete: boolean;
  };
  turns: Turn[];
  build?: BuildResult;
};

export type BuildResult = {
  ok: boolean;
  template: string;
  workspace: string;
  report: string;
  verification: {
    ok: boolean;
    tests_passed: boolean;
    test_output: string;
    files: string[];
    total_lines: number;
    violations: string[];
  };
  guard: Record<string, unknown>;
  cost_usd: number | null;
  duration_ms: number;
  is_error: boolean;
  errors: string[];
};

export type BuildStatus = {
  session_id: string;
  status: "none" | "running" | "ok" | "failed";
  result: (BuildResult & { error?: string }) | null;
};

export type EvalRun = {
  suite: string;
  run_id: string;
  ran_at: string | null;
  summary: Record<string, Record<string, number>>;
  calls: number;
};

export type EvalSuites = { suites: { suite: string; runs: EvalRun[] }[] };

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "Could not reach the server.");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json())?.detail ?? detail;
    } catch {
      // no JSON body
    }
    throw new ApiError(response.status, detail);
  }
  return response.json();
}

export function getSessions(): Promise<AdminSession[]> {
  return json<AdminSession[]>("/api/admin/sessions");
}

export async function getTrace(sessionId: string): Promise<Trace> {
  return normalize(await json<Partial<Trace>>(`/api/admin/sessions/${sessionId}/trace`));
}

/**
 * Fill in what older snapshots do not have.
 *
 * The snapshot format grew over nine build steps: `match` arrived in step 2, `open_gaps` in
 * step 4, `reviews` in step 5, `build` in step 6, `feedback` in step 8, and `origin` on turns
 * somewhere in between. Runs on disk are from every era, and they are the audit record, so
 * they are not rewritten — the absence is real and stays real. This is the one place that
 * knows the format has history, so nothing downstream has to.
 *
 * The types above describe the *normalized* shape, which is why the response is read as
 * `Partial<Trace>` first: a type assertion on `response.json()` is a claim, not a check, and
 * claiming every key exists is how this crashed the first time.
 */
function normalize(raw: Partial<Trace>): Trace {
  return {
    session_id: raw.session_id ?? "",
    model: raw.model ?? "unknown",
    turn: raw.turn ?? 0,
    total_cost_usd: raw.total_cost_usd ?? 0,
    is_complete: raw.is_complete ?? false,
    failure: raw.failure ?? null,
    match: raw.match ?? null,
    web_search: raw.web_search ?? null,
    is_ready_for_review: raw.is_ready_for_review ?? false,
    open_gaps: raw.open_gaps ?? {},
    reviews: raw.reviews ?? [],
    review_status: raw.review_status ?? "pending",
    feedback: raw.feedback ?? [],
    resumed: raw.resumed ?? false,
    canvas: {
      entries: raw.canvas?.entries ?? [],
      current: raw.canvas?.current ?? {},
      missing: raw.canvas?.missing ?? [],
      is_complete: raw.canvas?.is_complete ?? false,
    },
    turns: (raw.turns ?? []).map((turn) => ({
      ...turn,
      origin: turn.origin ?? "stakeholder",
      tool_calls: turn.tool_calls ?? [],
      assessments: turn.assessments ?? [],
      captured: turn.captured ?? [],
      errors: turn.errors ?? [],
    })),
    build: raw.build,
  };
}

export function getBuild(sessionId: string): Promise<BuildStatus> {
  return json<BuildStatus>(`/api/builds/${sessionId}`);
}

export function startBuild(sessionId: string, force = false): Promise<BuildStatus> {
  return json<BuildStatus>(`/api/builds/${sessionId}?force=${force}`, { method: "POST" });
}

export function getEvals(): Promise<EvalSuites> {
  return json<EvalSuites>("/api/admin/evals");
}

export function getEvalRun(suite: string, run: string): Promise<Record<string, unknown>> {
  return json(`/api/admin/evals/${suite}/${run}`);
}
