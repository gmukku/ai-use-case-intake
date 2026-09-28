"use client";

/**
 * One turn in the conversation.
 *
 * The agent's text arrives a few characters at a time, so while a reply is still streaming
 * this renders whatever has landed so far plus a blinking caret. A pause then reads as
 * thinking rather than as a broken page.
 */
export type Role = "you" | "agent";

export default function Message({
  role,
  text,
  streaming = false,
}: {
  role: Role;
  text: string;
  streaming?: boolean;
}) {
  const isYou = role === "you";

  return (
    <div className={`flex flex-col gap-1.5 ${isYou ? "items-end" : "items-start"}`}>
      <span className="px-1 text-xs font-medium tracking-wide text-muted uppercase">
        {role}
      </span>
      <div
        className={[
          "max-w-[42rem] rounded-2xl px-4 py-3 text-[15px] leading-relaxed whitespace-pre-wrap",
          isYou
            ? "bg-accent text-background"
            : "border border-line bg-surface text-foreground",
        ].join(" ")}
      >
        {text}
        {streaming && <span className="caret ml-0.5 font-mono">▍</span>}
      </div>
    </div>
  );
}
