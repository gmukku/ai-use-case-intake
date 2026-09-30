/**
 * Tests for the sign-in throttle.
 *
 * Every test drives the clock explicitly rather than sleeping: a rate limiter tested with real
 * time is either slow or flaky, and usually both.
 *
 * Run with `npm test`.
 */

import assert from "node:assert/strict";
import { beforeEach, describe, it } from "node:test";

import {
  SlidingWindow,
  checkLogin,
  clientIp,
  recordFailure,
  recordSuccess,
  resetLoginThrottle,
} from "./rate-limit.ts";

const T0 = 1_700_000_000_000;

function req(headers: Record<string, string> = {}): Request {
  return new Request("https://example.test/api/auth", { method: "POST", headers });
}

beforeEach(() => {
  delete process.env.BLUEPRINT_LOGIN_MAX_PER_USER;
  delete process.env.BLUEPRINT_LOGIN_MAX_PER_IP;
  delete process.env.BLUEPRINT_LOGIN_WINDOW_S;
  resetLoginThrottle();
});

describe("SlidingWindow", () => {
  it("allows up to the limit and then refuses", () => {
    const w = new SlidingWindow(3, 1000);
    for (let i = 0; i < 3; i++) {
      assert.equal(w.check("k", T0).allowed, true, `attempt ${i + 1} should be allowed`);
      w.fail("k", T0);
    }
    assert.equal(w.check("k", T0).allowed, false);
  });

  it("reports how long to wait, and the wait is never zero", () => {
    const w = new SlidingWindow(1, 60_000);
    w.fail("k", T0);
    const d = w.check("k", T0 + 30_000);
    assert.equal(d.allowed, false);
    assert.ok(d.allowed === false && d.retryAfterS === 30, `got ${JSON.stringify(d)}`);

    // One millisecond left still rounds up to a second: a Retry-After of 0 invites an
    // immediate retry that fails again.
    const edge = w.check("k", T0 + 59_999);
    assert.ok(edge.allowed === false && edge.retryAfterS === 1);
  });

  it("slides: old attempts fall out one at a time, not all at once", () => {
    const w = new SlidingWindow(2, 1000);
    w.fail("k", T0);
    w.fail("k", T0 + 400);
    assert.equal(w.check("k", T0 + 500).allowed, false);

    // The first hit has now aged out, the second has not — so exactly one slot is free. A
    // fixed window would have handed back the whole budget here, allowing a double burst.
    assert.equal(w.check("k", T0 + 1100).allowed, true);
    w.fail("k", T0 + 1100);
    assert.equal(w.check("k", T0 + 1100).allowed, false);
  });

  it("keeps keys apart", () => {
    const w = new SlidingWindow(1, 1000);
    w.fail("a", T0);
    assert.equal(w.check("a", T0).allowed, false);
    assert.equal(w.check("b", T0).allowed, true);
  });

  it("forgets a key on success", () => {
    const w = new SlidingWindow(1, 1000);
    w.fail("k", T0);
    assert.equal(w.check("k", T0).allowed, false);
    w.succeed("k");
    assert.equal(w.check("k", T0).allowed, true);
  });

  it("evicts to stay bounded, because the keys come from the attacker", () => {
    // An unbounded map keyed on a request header is a memory-exhaustion vector reachable by
    // anyone who can reach the login page.
    const w = new SlidingWindow(1, 60_000, 5);
    for (let i = 0; i < 500; i++) w.fail(`ip-${i}`, T0);
    // The most recent key must still be counted; the oldest are the ones dropped.
    assert.equal(w.check("ip-499", T0).allowed, false, "newest key was evicted");
    assert.equal(w.check("ip-0", T0).allowed, true, "oldest key should have been evicted");
  });

  it("evicts by recency, not by first sight", () => {
    // Map keeps a key's original insertion position when a value is overwritten, so the
    // busiest key would be evicted first unless it is deleted and re-set.
    //
    // The limit is 2 and "busy" reaches it, so eviction is *observable*: if "busy" survives it
    // is refused, and if it was wrongly evicted its attempts are forgiven and it is allowed.
    // An earlier version of this test used a limit of 5 and asserted `allowed === true`, which
    // holds in both worlds — it passed against a deliberately broken implementation and was
    // caught by mutating the ordering away, not by re-reading it.
    const w = new SlidingWindow(2, 60_000, 2);
    w.fail("busy", T0);
    w.fail("other", T0 + 1);
    w.fail("busy", T0 + 2); // touching "busy" must move it behind "other"
    w.fail("newcomer", T0 + 3); // size now exceeds the cap; the oldest goes

    assert.equal(
      w.check("busy", T0 + 3).allowed,
      false,
      "the busiest key was evicted, so an attacker gets a fresh budget by staying busy",
    );
    assert.equal(w.check("other", T0 + 3).allowed, true, "the least recent key should have gone");
  });
});

describe("clientIp", () => {
  it("takes the first entry of x-forwarded-for", () => {
    assert.equal(clientIp(req({ "x-forwarded-for": "203.0.113.5, 10.0.0.1" })), "203.0.113.5");
  });

  it("falls back to x-real-ip, then to a constant", () => {
    assert.equal(clientIp(req({ "x-real-ip": "203.0.113.9" })), "203.0.113.9");
    assert.equal(clientIp(req()), "unknown");
  });

  it("never returns an empty key", () => {
    // An empty string would collide with nothing and silently disable the per-IP budget.
    assert.equal(clientIp(req({ "x-forwarded-for": "" })), "unknown");
    assert.equal(clientIp(req({ "x-forwarded-for": "   ,10.0.0.1" })), "unknown");
  });
});

describe("the login budgets", () => {
  it("throttles one username across different addresses", () => {
    // The distributed attack: one account, many source addresses. Per-IP alone would miss it.
    process.env.BLUEPRINT_LOGIN_MAX_PER_USER = "3";
    process.env.BLUEPRINT_LOGIN_MAX_PER_IP = "100";
    for (let i = 0; i < 3; i++) recordFailure("dana", `10.0.0.${i}`, T0);
    assert.equal(checkLogin("dana", "10.0.0.99", T0).allowed, false);
  });

  it("throttles one address spraying many usernames", () => {
    // The other shape: many accounts, one source. Per-username alone would miss it.
    process.env.BLUEPRINT_LOGIN_MAX_PER_USER = "100";
    process.env.BLUEPRINT_LOGIN_MAX_PER_IP = "3";
    for (let i = 0; i < 3; i++) recordFailure(`user${i}`, "10.0.0.1", T0);
    assert.equal(checkLogin("someone-else", "10.0.0.1", T0).allowed, false);
  });

  it("counts a username that does not exist exactly like one that does", () => {
    // If only real accounts were counted, a fast 429 would mean "this username exists" and
    // hand back the enumeration oracle the decoy hash in authenticate() closes.
    process.env.BLUEPRINT_LOGIN_MAX_PER_USER = "2";
    for (let i = 0; i < 2; i++) recordFailure("no-such-person", "10.0.0.1", T0);
    assert.equal(checkLogin("no-such-person", "10.0.0.2", T0).allowed, false);
  });

  it("is case-insensitive on the username, so casing cannot buy a fresh budget", () => {
    process.env.BLUEPRINT_LOGIN_MAX_PER_USER = "2";
    recordFailure("dana", "10.0.0.1", T0);
    recordFailure("DANA", "10.0.0.1", T0);
    assert.equal(checkLogin("DaNa", "10.0.0.1", T0).allowed, false);
  });

  it("clears the username budget on success but not the address budget", () => {
    // An attacker holding one valid credential must not be able to reset an address's
    // allowance on demand.
    process.env.BLUEPRINT_LOGIN_MAX_PER_USER = "5";
    process.env.BLUEPRINT_LOGIN_MAX_PER_IP = "3";
    for (let i = 0; i < 3; i++) recordFailure("dana", "10.0.0.1", T0);
    recordSuccess("dana");
    assert.equal(checkLogin("dana", "10.0.0.2", T0).allowed, true, "username should be forgiven");
    assert.equal(checkLogin("dana", "10.0.0.1", T0).allowed, false, "address should not be");
  });

  it("recovers after the window passes", () => {
    process.env.BLUEPRINT_LOGIN_MAX_PER_USER = "2";
    process.env.BLUEPRINT_LOGIN_WINDOW_S = "60";
    recordFailure("dana", "10.0.0.1", T0);
    recordFailure("dana", "10.0.0.1", T0);
    assert.equal(checkLogin("dana", "10.0.0.1", T0).allowed, false);
    assert.equal(checkLogin("dana", "10.0.0.1", T0 + 61_000).allowed, true);
  });

  it("reads its limits at call time, not at import", () => {
    // A module-level constant would capture the default and ignore this, which is the
    // binding-time bug this project already paid for once in eval_runs.py.
    process.env.BLUEPRINT_LOGIN_MAX_PER_USER = "1";
    recordFailure("dana", "10.0.0.1", T0);
    assert.equal(checkLogin("dana", "10.0.0.1", T0).allowed, false);
  });

  it("falls back to the defaults for junk configuration rather than to no limit", () => {
    for (const bad of ["0", "-5", "abc", ""]) {
      resetLoginThrottle();
      process.env.BLUEPRINT_LOGIN_MAX_PER_USER = bad;
      for (let i = 0; i < 10; i++) recordFailure("dana", "10.0.0.1", T0);
      assert.equal(
        checkLogin("dana", "10.0.0.1", T0).allowed,
        false,
        `limit of ${JSON.stringify(bad)} should have fallen back to the default of 10`,
      );
    }
  });
});
