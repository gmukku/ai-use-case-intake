/**
 * The browser's view of the Blueprint AI API.
 *
 * Only the requester surface lives here: creating a session, sending a message, reading
 * status. Reviewer and admin routes need a bearer token, which must never reach browser
 * JavaScript, so those will go through a server-side route handler in step 9 instead.
 *
 * `sendMessage` and `resumeSession` are async generators that yield the same event
 * vocabulary the Python orchestrator yields (`text`, `activity`, `done`, `error`), so the
 * SSE wire format stops at this file and the components never see it.
 */

// Inlined into the bundle at build time. 127.0.0.1 rather than localhost on purpose: on
// Windows `localhost` can resolve to ::1 while uvicorn is listening on 127.0.0.1.
const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8000";

export type ReviewStatus = "pending" | "approved" | "rejected" | "sent_back";
export type BuildState = "none" | "running" | "ok" | "failed";

/** The requester-safe view of a session. Deliberately has no field for a category or risk. */
export type SessionStatus = {
  session_id: string;
  live: boolean;
  turn: number;
  is_complete: boolean;
  ready_for_review: boolean;
  review_status: ReviewStatus;
  build_status: BuildState;
  /** A reviewer's question is waiting and has not been asked yet. */
  can_reopen: boolean;
  outcome: string | null;
};

export type Rating = "up" | "down";

/** One side of one turn, as the requester saw it. */
export type Utterance = {
  turn: number;
  role: "you" | "agent";
  text: string;
};

/** The conversation, for restoring it after a reload. */
export type Transcript = {
  messages: Utterance[];
  /** Latest rating per turn, keyed by turn number as a string (JSON has no integer keys). */
  feedback: Record<string, Rating>;
};

/** One thing that happened during a turn. Mirrors `blueprint.orchestrator`'s events. */
export type TurnEvent =
  | { type: "text"; text: string }
  | { type: "activity" }
  | {
      type: "done";
      turn: number;
      is_complete: boolean;
      ready_for_review: boolean;
      error: string | null;
    }
  | { type: "error"; message: string };

/** The API answered, but not with success. Carries the status so callers can tell 409 apart. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    // fetch only rejects when the request never got an answer: server down, DNS, CORS.
    throw new ApiError(0, "Could not reach the server. Is the API running?");
  }
  if (!response.ok) {
    throw new ApiError(response.status, await errorDetail(response));
  }
  return response;
}

/** FastAPI puts its message in `detail`; fall back to the status text if the body is odd. */
async function errorDetail(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) return body.detail[0]?.msg ?? response.statusText;
  } catch {
    // no JSON body
  }
  return response.statusText || `request failed (${response.status})`;
}

export async function createSession(): Promise<string> {
  const response = await request("/sessions", { method: "POST" });
  const body: { session_id: string } = await response.json();
  return body.session_id;
}

export async function getStatus(sessionId: string): Promise<SessionStatus> {
  const response = await request(`/sessions/${sessionId}`);
  return response.json();
}

/** The conversation so far, so a reload or a shared link can pick it up where it left off. */
export async function getTranscript(sessionId: string): Promise<Transcript> {
  const response = await request(`/sessions/${sessionId}/messages`);
  return response.json();
}

/** Rate one agent turn. Append-only on the server; sending again simply supersedes. */
export async function rateTurn(
  sessionId: string,
  turn: number,
  rating: Rating,
  comment = "",
): Promise<void> {
  await request(`/sessions/${sessionId}/feedback`, {
    method: "POST",
    body: JSON.stringify({ turn, rating, comment }),
  });
}

export function sendMessage(sessionId: string, text: string): AsyncGenerator<TurnEvent> {
  return streamTurn(`/sessions/${sessionId}/messages`, JSON.stringify({ text }));
}

/** Reopen a session. If a reviewer sent it back, their question arrives as this turn. */
export function resumeSession(sessionId: string): AsyncGenerator<TurnEvent> {
  return streamTurn(`/sessions/${sessionId}/resume`, undefined);
}

/**
 * POST, then read the `text/event-stream` response frame by frame.
 *
 * The native `EventSource` API cannot be used here: it is GET-only with no request body,
 * and the stakeholder's message belongs in a body, not in a URL.
 */
async function* streamTurn(path: string, body: string | undefined): AsyncGenerator<TurnEvent> {
  const response = await request(path, { method: "POST", body });
  if (!response.body) {
    throw new ApiError(0, "The server sent no stream to read.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  // Chunk boundaries fall wherever the network puts them, so a frame can arrive split in
  // half. Everything after the last blank line stays here until its terminator shows up.
  let buffer = "";

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      let split: number;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, split);
        buffer = buffer.slice(split + 2);
        const event = parseFrame(frame);
        if (event) yield event;
      }
    }
  } finally {
    // Leaving a turn early (the user navigates away) must not leave the socket open.
    await reader.cancel().catch(() => {});
  }
}

/** Turn one `event: <name>\ndata: <json>` frame into a typed event, or null if unknown. */
function parseFrame(frame: string): TurnEvent | null {
  let name = "";
  let data = "{}";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) name = line.slice(7);
    else if (line.startsWith("data: ")) data = line.slice(6);
  }

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(data);
  } catch {
    return { type: "error", message: "The server sent something unreadable." };
  }

  switch (name) {
    case "text":
      return { type: "text", text: String(payload.text ?? "") };
    case "activity":
      return { type: "activity" };
    case "done":
      return {
        type: "done",
        turn: Number(payload.turn ?? 0),
        is_complete: Boolean(payload.is_complete),
        ready_for_review: Boolean(payload.ready_for_review),
        error: (payload.error as string | null) ?? null,
      };
    case "error":
      return { type: "error", message: String(payload.message ?? "Something went wrong.") };
    default:
      return null; // an event we don't know yet is not a reason to break the turn
  }
}
