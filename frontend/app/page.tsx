"use client";

import { useEffect, useRef, useState } from "react";

import Composer from "./components/Composer";
import Message, { type Role } from "./components/Message";
import { ApiError, createSession, getStatus, sendMessage } from "@/lib/api";

type Entry = { role: Role; text: string };

export default function Home() {
  // Finished turns, oldest first. Replaced, never mutated — React notices a new array.
  const [entries, setEntries] = useState<Entry[]>([]);
  // The reply currently landing, character by character. Becomes an Entry when the turn ends.
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);
  const [closed, setClosed] = useState(false);

  // A ref holds a value across renders without causing one. The session id changes nothing
  // on screen, so it does not belong in state.
  const sessionId = useRef<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  // Effects run after the browser has painted, which is when there is something to scroll to.
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [entries, reply]);

  async function send(text: string) {
    setError(null);
    setEntries((prev) => [...prev, { role: "you", text }]);
    setBusy(true);

    let landed = "";
    try {
      // Sessions are created lazily: each one owns an SDK subprocess, so opening the page
      // should not start one.
      sessionId.current ??= await createSession();

      for await (const event of sendMessage(sessionId.current, text)) {
        switch (event.type) {
          case "text":
            landed += event.text;
            // Functional form: the previous value comes from React, not from a variable
            // captured when this loop started.
            setReply((prev) => prev + event.text);
            break;
          case "activity":
            // Something is happening that the requester has no reason to see the name of.
            // The indicator is already up; this event only confirms it should stay up.
            break;
          case "done":
            if (event.error) setError(event.error);
            if (event.ready_for_review) {
              setClosed(true);
              const status = await getStatus(sessionId.current);
              setOutcome(status.outcome);
            }
            break;
          case "error":
            setError(event.message);
            break;
        }
      }
    } catch (failure) {
      setError(
        failure instanceof ApiError
          ? failure.message
          : "Something went wrong. Your answers so far are saved.",
      );
    } finally {
      if (landed) setEntries((prev) => [...prev, { role: "agent", text: landed }]);
      setReply("");
      setBusy(false);
    }
  }

  const empty = entries.length === 0 && !reply;
  // The gap between pressing send and the first character is several seconds: creating the
  // session, then the model deciding what to ask. Silence there reads as a broken page, so
  // the indicator is tied to the turn being open, not to an activity ping arriving.
  const thinking = busy && !reply;

  return (
    <main className="mx-auto flex w-full max-w-3xl flex-1 flex-col px-5 py-8">
      <header className="mb-8 border-b border-line pb-6">
        <h1 className="text-xl font-semibold tracking-tight">Blueprint AI</h1>
        <p className="mt-1.5 text-sm text-muted">
          Describe something you&apos;d like to automate, in your own words. A few questions
          later, we&apos;ll write it up for a human to review.
        </p>
      </header>

      <div className="flex flex-1 flex-col gap-6">
        {empty && (
          <p className="text-[15px] leading-relaxed text-muted">
            Start anywhere — what takes too long today, who it slows down, what you wish
            happened instead.
          </p>
        )}

        {entries.map((entry, i) => (
          <Message key={i} role={entry.role} text={entry.text} />
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

        {outcome && (
          <p className="rounded-xl border border-line bg-surface px-4 py-3 text-[15px] leading-relaxed">
            {outcome}
          </p>
        )}

        <div ref={bottom} />
      </div>

      <div className="sticky bottom-0 mt-8 bg-background pt-4 pb-2">
        <Composer
          onSend={send}
          disabled={busy || closed}
          placeholder={
            closed ? "This conversation is with a reviewer now." : "Type your answer…"
          }
        />
      </div>
    </main>
  );
}
