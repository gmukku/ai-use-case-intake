import type { Page, Route } from "@playwright/test";

/**
 * A fake Blueprint API, installed into the browser with `page.route`.
 *
 * It speaks the same wire format as `blueprint.api`: JSON for status and transcript, and
 * `text/event-stream` for a turn. The frame shape is the contract — `event: <name>` and
 * `data: <json>`, separated by a blank line — and it is reproduced here rather than imported
 * so that a change to either side shows up as a failing test instead of silently agreeing
 * with itself.
 */

export type StatusOverrides = Partial<{
  live: boolean;
  turn: number;
  is_complete: boolean;
  ready_for_review: boolean;
  review_status: "pending" | "approved" | "rejected" | "sent_back";
  build_status: "none" | "running" | "ok" | "failed";
  can_reopen: boolean;
  outcome: string | null;
}>;

export type Utterance = { turn: number; role: "you" | "agent"; text: string };

/** One SSE frame, exactly as the server writes it. */
export function frame(event: string, data: Record<string, unknown> = {}): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/** The frames of an ordinary successful turn. */
export function turn(text: string, opts: { turn?: number; complete?: boolean } = {}): string[] {
  return [
    frame("text", { text }),
    frame("done", {
      turn: opts.turn ?? 1,
      is_complete: opts.complete ?? false,
      ready_for_review: opts.complete ?? false,
      error: null,
    }),
  ];
}

export class ApiStub {
  readonly sessionId = "e2e-session";
  private status: Required<StatusOverrides>;
  private transcript: Utterance[] = [];
  /** Frames for the next turn, in order. A caller can split one frame across several. */
  private script: string[] = [];
  private delayMs = 0;
  /** Turn requests seen, so a test can assert a click did or did not reach the server. */
  readonly calls: string[] = [];

  constructor(overrides: StatusOverrides = {}) {
    this.status = {
      live: true,
      turn: 0,
      is_complete: false,
      ready_for_review: false,
      review_status: "pending",
      build_status: "none",
      can_reopen: false,
      outcome: null,
      ...overrides,
    };
  }

  setStatus(overrides: StatusOverrides): this {
    this.status = { ...this.status, ...overrides };
    return this;
  }

  setTranscript(messages: Utterance[]): this {
    this.transcript = messages;
    return this;
  }

  /** What the next turn will stream. Each string is written as its own network chunk. */
  setScript(chunks: string[], delayMs = 0): this {
    this.script = chunks;
    this.delayMs = delayMs;
    return this;
  }

  async install(page: Page): Promise<void> {
    await page.route("**/sessions**", async (route: Route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      const method = request.method();
      this.calls.push(`${method} ${path}`);

      if (path === "/sessions" && method === "POST") {
        return route.fulfill({ json: { session_id: this.sessionId } });
      }
      // GET and POST share this path and mean different things: read the transcript, or
      // take a turn. Branching on the method is the whole difference.
      if (path.endsWith("/messages") && method === "GET") {
        return route.fulfill({ json: { messages: this.transcript, feedback: {} } });
      }
      if (path.endsWith("/feedback")) {
        return route.fulfill({ status: 204, body: "" });
      }
      if (path.endsWith("/messages") || path.endsWith("/resume")) {
        return this.stream(route);
      }
      if (method === "GET") {
        return route.fulfill({ json: { session_id: this.sessionId, ...this.status } });
      }
      return route.fulfill({ status: 404, json: { detail: "no stub for this route" } });
    });
  }

  /**
   * Write the scripted chunks as one `text/event-stream` body.
   *
   * Playwright fulfils a route with a whole body rather than a live stream, so "chunks" are
   * concatenated here. That is still a real test of the reader: what matters is that a frame
   * can be split anywhere, and a caller splits them wherever it likes.
   */
  private async stream(route: Route): Promise<void> {
    if (this.delayMs) await new Promise((r) => setTimeout(r, this.delayMs));
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream", "cache-control": "no-cache" },
      body: this.script.join(""),
    });
  }
}

/** Fail a specific call, to test the error paths. */
export async function failRoute(page: Page, match: string, status: number, detail: string) {
  await page.route(`**${match}**`, (route) => route.fulfill({ status, json: { detail } }));
}
