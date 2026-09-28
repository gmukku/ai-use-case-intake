"use client";

import { useState } from "react";

import type { Rating } from "@/lib/api";

/**
 * Two thumbs under an agent turn.
 *
 * The rating is sent immediately — asking for a comment first would cost the signal, since
 * almost nobody writes one. A thumbs-down then offers the box, because that is the case
 * where the reason is worth more than the rating.
 */
export default function Feedback({
  current,
  onRate,
}: {
  current: Rating | null;
  onRate: (rating: Rating, comment?: string) => void;
}) {
  const [asking, setAsking] = useState(false);
  const [comment, setComment] = useState("");

  function rate(rating: Rating) {
    onRate(rating);
    setAsking(rating === "down");
  }

  function submitComment() {
    if (!comment.trim()) return;
    onRate("down", comment.trim());
    setComment("");
    setAsking(false);
  }

  return (
    <div className="flex flex-col gap-2 px-1">
      <div className="flex items-center gap-1">
        {(["up", "down"] as const).map((rating) => (
          <button
            key={rating}
            type="button"
            onClick={() => rate(rating)}
            aria-label={rating === "up" ? "This was helpful" : "This missed"}
            aria-pressed={current === rating}
            className={`rounded-lg px-2 py-1 text-sm transition-colors ${
              current === rating
                ? "bg-accent/15 text-accent"
                : "text-muted hover:bg-line/60 hover:text-foreground"
            }`}
          >
            {rating === "up" ? "👍" : "👎"}
          </button>
        ))}
      </div>

      {asking && (
        <div className="flex items-center gap-2">
          <input
            value={comment}
            autoFocus
            placeholder="What was off? (optional)"
            aria-label="What was off about this reply"
            onChange={(event) => setComment(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") submitComment();
              if (event.key === "Escape") setAsking(false);
            }}
            className="flex-1 rounded-lg border border-line bg-surface px-3 py-1.5 text-sm outline-none placeholder:text-muted"
          />
          <button
            type="button"
            onClick={submitComment}
            className="rounded-lg px-2 py-1.5 text-sm text-muted hover:text-foreground"
          >
            Add
          </button>
        </div>
      )}
    </div>
  );
}
