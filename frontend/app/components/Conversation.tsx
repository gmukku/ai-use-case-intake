"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Composer from "./Composer";
import Feedback from "./Feedback";
import Message, { type Role } from "./Message";
import {
  ApiError,
  type Rating,
  type SessionStatus,
  type TurnEvent,
  createSession,
  getStatus,
  getTranscript,
  rateTurn,
  resumeSession,
  sendMessage,
} from "@/lib/api";

type Entry = { turn: number; role: Role; text: string };

/** How often to re-check a closed session. A reviewer decides on human time, not machine time. */
const POLL_MS = 8000;

export default function Conversation({ initialSessionId }: { initialSessionId: string | null }) {
  const [entries, setEntries] = useState<Entry[]>([]);
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<SessionStatus | null>(null);
  const [ratings, setRatings] = useState<Record<string, Rating>>({});
  const [ready, setReady] = useState(false);
  const [restoring, setRestoring] = useState(initialSessionId !== null);

  const sessionId = useRef<string | null>(initialSessionId);
  const bottom = useRef<HTMLDivElement>(null);
  const scrolledOnce = useRef(false);

  useEffect(() => {
    if (!entries.length && !reply) return;
    // `block: "end"` puts the sentinel at the *bottom* of the viewport. The default is
    // "start", which scrolls it to the top — fine with two messages, and a blank screen
    // once a transcript is long enough to have nothing below the last one.
    bottom.current?.scrollIntoView({
      behavior: scrolledOnce.current ? "smooth" : "instant",
      block: "end",
    });
    // A restored transcript should just be *there*; only new messages are worth animating.
    scrolledOnce.current = true;
  }, [entries, reply]);

  // Landing on /s/<id> — a refresh, a bookmark, or a link back after a reviewer's question.
  // The transcript lives on the server, so it survives the browser losing everything.
  useEffect(() => {
    if (!initialSessionId) return;
    let cancelled = false;

    (async () => {
      try {
        const [transcript, current] = await Promise.all([
          getTranscript(initialSessionId),
          getStatus(initialSessionId),
        ]);
        if (cancelled) return;
        setEntries(transcript.messages.map((m) => ({ turn: m.turn, role: m.role, text: m.text })));
        setRatings(transcript.feedback);
        setStatus(current);
        setReady(current.ready_for_review);
      } catch (failure) {
        if (!cancelled) {
          setError(
            failure instanceof ApiError && failure.status === 404
              ? "That conversation no longer exists."
              : "Could not load that conversation.",
          );
        }
      } finally {
        if (!cancelled) setRestoring(false);
      }
    })();

    // Cleanup runs if the component unmounts mid-request, so a late response cannot write
    // into state that no longer exists.
    return () => {
      cancelled = true;
    };
  }, [initialSessionId]);

  // Not "was it sent back" but "is a question still waiting": review_status stays
  // sent_back after the agent asks, so it cannot be the thing that offers to reopen.
  const canReopen = status?.can_reopen ?? false;
  const settled =
    status !== null &&
    (status.review_status === "rejected" ||
      (status.review_status === "approved" && status.build_status !== "running"));

  // While a session sits with a reviewer, the answer arrives on someone else's schedule.
  // Polling stops once the outcome can no longer change.
  useEffect(() => {
    const id = sessionId.current;
    if (!id || !ready || busy || settled) return;

    const timer = setInterval(async () => {
      try {
        setStatus(await getStatus(id));
      } catch {
        // A poll that fails is not worth showing; the next one will say the same thing.
      }
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [ready, busy, settled]);

  const consume = useCallback(async (events: AsyncGenerator<TurnEvent>) => {
    let landed = "";
    let turn = 0;
    try {
      for await (const event of events) {
        switch (event.type) {
          case "text":
            landed += event.text;
            // Functional form: the previous value comes from React, not from a variable
            // captured when this loop started.
            setReply((prev) => prev + event.text);
            break;
          case "activity":
            break; // something is happening the requester has no reason to see the name of
          case "done":
            turn = event.turn;
            if (event.error) setError(event.error);
            if (event.ready_for_review) setReady(true);
            break;
          case "error":
            setError(event.message);
            break;
        }
      }
    } finally {
      if (landed) setEntries((prev) => [...prev, { turn, role: "agent", text: landed }]);
      setReply("");
    }
  }, []);

  async function send(text: string) {
    setError(null);
    setBusy(true);
    setEntries((prev) => [...prev, { turn: prev.length, role: "you", text }]);
    try {
      if (!sessionId.current) {
        sessionId.current = await createSession();
        // Put the id in the URL without a navigation, so this component keeps its state and
        // a refresh still lands somewhere real. Next.js integrates with the native History
        // API for exactly this.
        window.history.replaceState(null, "", `/s/${sessionId.current}`);
      }
      await consume(sendMessage(sessionId.current, text));
      setStatus(await getStatus(sessionId.current));
    } catch (failure) {
      setError(describe(failure));
    } finally {
      setBusy(false);
    }
  }

  async function reopen() {
    const id = sessionId.current;
    if (!id) return;
    setError(null);
    setBusy(true);
    try {
      await consume(resumeSession(id));
      setReady(false);
      setStatus(await getStatus(id));
    } catch (failure) {
      setError(describe(failure));
    } finally {
      setBusy(false);
    }
  }

  async function rate(turn: number, rating: Rating, comment?: string) {
    const id = sessionId.current;
    if (!id) return;
    // Optimistic: the thumb fills in at once. A failed write is put back rather than left
    // showing a rating the server never received.
    const previous = ratings[turn];
    setRatings((prev) => ({ ...prev, [turn]: rating }));
    try {
      await rateTurn(id, turn, rating, comment ?? "");
    } catch {
      setRatings((prev) => {
        const next = { ...prev };
        if (previous) next[turn] = previous;
        else delete next[turn];
        return next;
      });
      setError("Could not save that — the rest of your conversation is unaffected.");
    }
  }

  // Two different questions, and conflating them locked the requester out of answering:
  //   canReopen  — is a reviewer's question still waiting to be asked? (offers the button)
  //   sentBack   — does the conversation belong to the requester again? (enables the box)
  // After the question is asked, canReopen goes false while sentBack stays true.
  const sentBack = status?.review_status === "sent_back";
  const closed = ready && !sentBack;
  const thinking = busy && !reply;
  const empty = entries.length === 0 && !reply && !restoring;

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col px-5 py-8">
      <header className="mb-8 border-b border-line pb-6">
        <h1 className="text-xl font-semibold tracking-tight">Blueprint AI</h1>
        <p className="mt-1.5 text-sm text-muted">
          Describe something you&apos;d like to automate, in your own words. A few questions
          later, we&apos;ll write it up for a human to review.
        </p>
      </header>

      {/* The composer is sticky, so it floats over whatever is at the bottom of the scroll.
          This padding is the space it needs; without it the last thing on the page is
          unreachable — which hid the reopen button completely. */}
      <div className="flex flex-1 flex-col gap-6 pb-28">
        {restoring && <p className="text-sm text-muted">Picking up where you left off…</p>}

        {empty && (
          <p className="text-[15px] leading-relaxed text-muted">
            Start anywhere — what takes too long today, who it slows down, what you wish
            happened instead.
          </p>
        )}

        {entries.map((entry, i) => (
          <div key={i} className="flex flex-col gap-1.5">
            <Message role={entry.role} text={entry.text} />
            {entry.role === "agent" && entry.turn > 0 && (
              <Feedback
                current={ratings[entry.turn] ?? null}
                onRate={(rating, comment) => rate(entry.turn, rating, comment)}
              />
            )}
          </div>
        ))}

        {reply && <Message role="agent" text={reply} streaming />}

        {thinking && (
          <p className="px-1 text-sm text-muted" aria-live="polite">
            <span className="caret">●</span> Thinking…
          </p>
        )}

        {error && (
          <p
            role="alert"
            className="rounded-xl border border-red-300/60 bg-red-50 px-4 py-3 text-sm text-red-900 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
          >
            {error}
          </p>
        )}

        {status?.outcome && !busy && (
          <div className="rounded-xl border border-line bg-surface px-4 py-3">
            <p className="text-[15px] leading-relaxed">{status.outcome}</p>
            {canReopen && (
              <button
                type="button"
                onClick={reopen}
                disabled={busy}
                className="mt-3 rounded-xl bg-accent px-4 py-2 text-sm font-medium text-background transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                Continue the conversation
              </button>
            )}
          </div>
        )}

        {/* scroll-mb keeps this sentinel clear of the sticky composer: scrollIntoView honours
            scroll-margin, so "the bottom" means above the bar rather than behind it. */}
        <div ref={bottom} className="scroll-mb-28" />
      </div>

      <div className="sticky bottom-0 mt-8 bg-background pt-4 pb-2">
        <Composer
          onSend={send}
          disabled={busy || closed || restoring}
          placeholder={
            closed ? "This conversation is with a reviewer now." : "Type your answer…"
          }
        />
      </div>
    </main>
  );
}

function describe(failure: unknown): string {
  if (failure instanceof ApiError) return failure.message;
  return "Something went wrong. Your answers so far are saved.";
}
