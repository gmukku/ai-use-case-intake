"use client";

import { useRef, useState, type KeyboardEvent } from "react";

/**
 * The message box.
 *
 * A *controlled input*: React owns the text and the textarea only displays it. The value
 * comes from state and every keystroke goes back through `setText`, so there is exactly one
 * copy of the truth. Reading the text off the DOM when the button is clicked would work
 * today and drift the moment anything else needs to change it.
 */
export default function Composer({
  onSend,
  disabled,
  placeholder,
}: {
  onSend: (text: string) => void;
  disabled: boolean;
  placeholder: string;
}) {
  const [text, setText] = useState("");
  const box = useRef<HTMLTextAreaElement>(null);

  const ready = text.trim().length > 0 && !disabled;

  function submit() {
    if (!ready) return;
    onSend(text.trim());
    setText("");
    resize(box.current);
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends, Shift+Enter makes a newline — the convention every chat box uses.
    // preventDefault stops the browser from also inserting the newline we just intercepted.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="flex items-end gap-2 rounded-2xl border border-line bg-surface p-2 shadow-sm">
      <textarea
        ref={box}
        rows={1}
        value={text}
        disabled={disabled}
        placeholder={placeholder}
        aria-label="Your message"
        onChange={(event) => {
          setText(event.target.value);
          resize(event.target);
        }}
        onKeyDown={onKeyDown}
        className="max-h-48 flex-1 resize-none bg-transparent px-3 py-2 text-[15px] leading-relaxed outline-none placeholder:text-muted disabled:cursor-not-allowed disabled:opacity-60"
      />
      <button
        type="button"
        onClick={submit}
        disabled={!ready}
        className="rounded-xl bg-accent px-4 py-2 text-sm font-medium text-background transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40"
      >
        Send
      </button>
    </div>
  );
}

/**
 * Grow the box with its content, up to the CSS max height.
 *
 * This is the case a ref is actually for: measuring and setting a real DOM property that
 * React has no state equivalent for. Height is reset to `auto` first so the element can
 * shrink again — `scrollHeight` never reports less than the current height.
 */
function resize(element: HTMLTextAreaElement | null) {
  if (!element) return;
  element.style.height = "auto";
  element.style.height = `${element.scrollHeight}px`;
}
