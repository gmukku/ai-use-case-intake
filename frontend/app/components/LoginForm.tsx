"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

export default function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(
    params.get("error") === "unconfigured"
      ? "Sign-in is not configured on this server yet."
      : params.get("error") === "forbidden"
        ? "That surface needs an admin account."
        : null,
  );
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const response = await fetch("/api/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        setError(body.detail ?? "Sign-in failed.");
        return;
      }
      // Where the proxy was sending them before it stopped them.
      const next = params.get("next");
      router.replace(next && next.startsWith("/") ? next : "/review");
      router.refresh();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto flex w-full max-w-sm flex-1 flex-col justify-center gap-6 px-5 py-8">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Blueprint AI</h1>
        <p className="mt-1.5 text-sm text-muted">
          Reviewer and admin sign-in. If you came here to describe an idea, you want{" "}
          <Link href="/" className="underline underline-offset-2">
            the conversation
          </Link>{" "}
          instead — no account needed.
        </p>
      </header>

      <form onSubmit={submit} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1.5 text-sm">
          Username
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            autoFocus
            className="rounded-lg border border-line bg-surface px-3 py-2 outline-none"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            className="rounded-lg border border-line bg-surface px-3 py-2 outline-none"
          />
        </label>

        {error && (
          <p
            role="alert"
            className="rounded-lg border border-red-300/60 bg-red-50 px-3 py-2 text-sm text-red-900 dark:border-red-900/50 dark:bg-red-950/40 dark:text-red-200"
          >
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy || !username || !password}
          className="rounded-xl bg-accent px-4 py-2 text-sm font-medium text-background disabled:opacity-40"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
