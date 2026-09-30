import "server-only";

/**
 * A sliding-window counter for throttling sign-in attempts.
 *
 * In-process and unshared, which is a real limit rather than an oversight: with two Next
 * instances an attacker gets two budgets. That matches the API's documented single-process
 * deployment (see the session registry), and the moment either becomes multi-process this
 * needs a shared store, not a bigger number.
 *
 * Sliding rather than fixed windows: a fixed window lets an attacker spend a full budget at
 * the end of one window and another at the start of the next, so the real burst is double the
 * configured limit at exactly the moment someone is trying.
 */

export type Decision = { allowed: true } | { allowed: false; retryAfterS: number };

/**
 * Bounded, recency-ordered store of recent hit timestamps per key.
 *
 * The bound is the point. Keys come from request headers, so an attacker chooses them; an
 * unbounded map is a memory-exhaustion vector reachable by anyone who can reach the login
 * page. Entries are pruned when touched, and the oldest are evicted once `maxKeys` is
 * reached. Eviction is safe in the direction that matters: losing an old entry forgives past
 * attempts, it never invents new ones.
 */
export class SlidingWindow {
  private readonly hits = new Map<string, number[]>();
  private readonly limit: number;
  private readonly windowMs: number;
  private readonly maxKeys: number;

  // Written out rather than as constructor parameter properties: those emit assignments, and
  // Node's built-in TypeScript support only strips types, so `npm test` cannot load them.
  constructor(limit: number, windowMs: number, maxKeys = 10_000) {
    this.limit = limit;
    this.windowMs = windowMs;
    this.maxKeys = maxKeys;
  }

  /** Is this key under its limit? Does not count the attempt; call {@link fail} for that. */
  check(key: string, now: number = Date.now()): Decision {
    const recent = this.recent(key, now);
    if (recent.length < this.limit) return { allowed: true };
    // The window clears when the oldest hit in it falls out, so that is when to come back.
    const retryAfterMs = recent[0] + this.windowMs - now;
    return { allowed: false, retryAfterS: Math.max(1, Math.ceil(retryAfterMs / 1000)) };
  }

  /** Count one failed attempt. */
  fail(key: string, now: number = Date.now()): void {
    const recent = this.recent(key, now);
    recent.push(now);
    // Delete before set so insertion order tracks recency: Map keeps a key's original
    // position on overwrite, which would make eviction drop whatever was busiest.
    this.hits.delete(key);
    this.hits.set(key, recent);
    this.evict();
  }

  /** Forget a key's failures. Called on a successful sign-in. */
  succeed(key: string): void {
    this.hits.delete(key);
  }

  /** Timestamps still inside the window, pruned of everything older. */
  private recent(key: string, now: number): number[] {
    const cutoff = now - this.windowMs;
    return (this.hits.get(key) ?? []).filter((t) => t > cutoff);
  }

  private evict(): void {
    while (this.hits.size > this.maxKeys) {
      const oldest = this.hits.keys().next();
      if (oldest.done) return;
      this.hits.delete(oldest.value);
    }
  }

  /** Test seam. */
  reset(): void {
    this.hits.clear();
  }
}

// -- configuration --------------------------------------------------------------------------

function num(name: string, fallback: number): number {
  // Read at call time, never bound at module load: a default captured at import cannot be
  // overridden by a test, which is a bug this project has already paid for once in eval_runs.
  const raw = process.env[name];
  const value = raw ? Number(raw) : NaN;
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

/**
 * Attempts allowed per window.
 *
 * scrypt already makes each guess cost real CPU, so these are set to stop sustained grinding
 * rather than to punish someone mistyping a password twice. The per-IP budget is deliberately
 * looser than the per-user one because a single address may legitimately be a whole office
 * behind one NAT.
 */
export const limits = () => ({
  perUser: num("BLUEPRINT_LOGIN_MAX_PER_USER", 10),
  perIp: num("BLUEPRINT_LOGIN_MAX_PER_IP", 30),
  windowMs: num("BLUEPRINT_LOGIN_WINDOW_S", 900) * 1000,
});

let windows: { user: SlidingWindow; ip: SlidingWindow; windowMs: number } | null = null;

function counters() {
  const { perUser, perIp, windowMs } = limits();
  if (!windows || windows.windowMs !== windowMs) {
    windows = {
      user: new SlidingWindow(perUser, windowMs),
      ip: new SlidingWindow(perIp, windowMs),
      windowMs,
    };
  }
  return windows;
}

/** Test seam: drop all recorded attempts. */
export function resetLoginThrottle(): void {
  windows = null;
}

/**
 * Best-effort client address.
 *
 * Honest about what this is worth: `x-forwarded-for` is a request header, so without a proxy
 * that overwrites it, an attacker sets it per request and the per-IP budget is free to
 * bypass. That is exactly why the per-username budget exists — it keys on something the
 * attacker must hold constant to make progress against an account. Treat per-IP as
 * defence in depth, not as the control.
 */
export function clientIp(request: Request): string {
  const forwarded = request.headers.get("x-forwarded-for");
  const first = forwarded?.split(",")[0]?.trim();
  return first || request.headers.get("x-real-ip")?.trim() || "unknown";
}

/**
 * Check both budgets before doing any password work.
 *
 * Keyed on the submitted username whether or not that account exists. Counting only real
 * accounts would make a fast 429 mean "this username is real", handing back the enumeration
 * oracle that `authenticate`'s decoy hash is there to close.
 */
export function checkLogin(username: string, ip: string, now: number = Date.now()): Decision {
  const { user, ip: ipWindow } = counters();
  const byUser = user.check(username.toLowerCase(), now);
  if (!byUser.allowed) return byUser;
  return ipWindow.check(ip, now);
}

/** Record a failed sign-in against both budgets. */
export function recordFailure(username: string, ip: string, now: number = Date.now()): void {
  const { user, ip: ipWindow } = counters();
  user.fail(username.toLowerCase(), now);
  ipWindow.fail(ip, now);
}

/**
 * Clear the username's budget after a success.
 *
 * The IP's budget is deliberately left alone. One valid credential would otherwise reset the
 * address's allowance on demand, which is the whole budget defeated by an attacker who has
 * compromised any single account.
 */
export function recordSuccess(username: string): void {
  counters().user.succeed(username.toLowerCase());
}
