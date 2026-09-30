/**
 * The reviewer surface, from the browser's side.
 *
 * Every path here is same-origin (`/api/...`), not the FastAPI base URL. Those route
 * handlers run on the server and hold the admin token; nothing in this file knows it exists.
 */

import { ApiError } from "./api";

export type RiskLevel = "low" | "elevated" | "high";

export type QueueItem = {
  session_id: string;
  title: string;
  risk_level: RiskLevel;
  needs_extra_scrutiny: boolean;
  review_status: string;
  turns: number;
  ready_for_review: boolean;
  live: boolean;
};

export type RiskFlag = {
  key: string;
  level: RiskLevel;
  category: string | null;
  evidence: string;
  note: string;
};

export type SpecCategory = {
  category: string;
  label: string;
  summary: string;
  version: number;
  sufficient: boolean;
  missing: string[];
};

export type Spec = {
  version: number;
  session_id: string;
  title: string;
  narrative: string;
  /**
   * Readiness classifications, null when the spec was never summarised — and absent entirely
   * on specs stored before they existed, so treat a missing key the same as null.
   */
  samples: "offered" | "unavailable" | "not_discussed" | null;
  impact: "quantified" | "named_only" | "none_stated" | null;
  categories: SpecCategory[];
  departments: string[];
  department_rationale: string | null;
  sops_consulted: string[];
  web_sources: string[];
  open_gaps: Record<string, string[]>;
  risk: { level: RiskLevel; needs_extra_scrutiny: boolean; flags: RiskFlag[] };
  turns: number;
  total_cost_usd: number;
  compiled_at: string;
};

export type Decision = {
  action: string;
  reviewer: string;
  note: string;
  spec_version: number;
  risk_level: RiskLevel;
  decided_at: string;
};

export type ReviewDetail = {
  spec: Spec;
  review_status: string;
  reviews: Decision[];
};

export type Action = "approve" | "reject" | "send_back";

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
  return response.status === 204 ? (undefined as T) : response.json();
}

export function getQueue(): Promise<QueueItem[]> {
  return json<QueueItem[]>("/api/reviews");
}

export function getReview(sessionId: string, summarize = false): Promise<ReviewDetail> {
  return json<ReviewDetail>(`/api/reviews/${sessionId}?summarize=${summarize}`);
}

export function decide(
  sessionId: string,
  action: Action,
  note: string,
): Promise<{ review_status: string; spec_version: number }> {
  return json(`/api/reviews/${sessionId}/decision`, {
    method: "POST",
    body: JSON.stringify({ action, note }),
  });
}

export function getReviewer(): Promise<{ reviewer: string | null }> {
  return json<{ reviewer: string | null }>("/api/reviewer");
}

export function signIn(name: string): Promise<{ reviewer: string }> {
  return json<{ reviewer: string }>("/api/reviewer", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
}
