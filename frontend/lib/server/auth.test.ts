/**
 * Tests for the auth core.
 *
 * This is the most security-sensitive file in the repo and it shipped with a real bug: a
 * deleted user's session kept full admin access, because a signed cookie was never checked
 * against the account store. That bug was found by poking a running server. These exist so
 * the next one is found here instead.
 *
 * Run with `npm test`. `--conditions=react-server` resolves the `server-only` import to an
 * empty module, which is exactly what Next does when it loads this file on the server — so
 * the module under test is imported the same way it runs in production, not a mocked copy.
 */

import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, before, beforeEach, describe, it } from "node:test";

import {
  type Role,
  type Session,
  allows,
  authenticate,
  hashPassword,
  isConfigured,
  issueSession,
  readSession,
} from "./auth.ts";

const SECRET = "test-secret-not-used-anywhere-else";
let dir: string;
let usersFile: string;

/** Write a user store and point the module at it. */
async function setUsers(...users: { username: string; role: Role; password?: string }[]) {
  const written = [];
  for (const u of users) {
    written.push({
      username: u.username,
      name: u.username.toUpperCase(),
      role: u.role,
      password: await hashPassword(u.password ?? "correct-horse-battery"),
    });
  }
  writeFileSync(usersFile, JSON.stringify(written, null, 2));
}

function session(username = "dana", role: Role = "reviewer"): Session {
  return { username, name: username.toUpperCase(), role };
}

/** A cookie for a session that the user store agrees with. */
function cookieFor(s: Session): string {
  const token = issueSession(s);
  assert.ok(token, "issueSession returned null; is the secret set?");
  return token;
}

before(() => {
  dir = mkdtempSync(join(tmpdir(), "blueprint-auth-"));
  usersFile = join(dir, "users.json");
});

after(() => rmSync(dir, { recursive: true, force: true }));

beforeEach(() => {
  process.env.BLUEPRINT_SESSION_SECRET = SECRET;
  process.env.BLUEPRINT_USERS_FILE = usersFile;
});

describe("allows", () => {
  it("admin satisfies both roles", () => {
    assert.equal(allows("admin", "reviewer"), true);
    assert.equal(allows("admin", "admin"), true);
  });

  it("reviewer does not reach admin", () => {
    assert.equal(allows("reviewer", "reviewer"), true);
    assert.equal(allows("reviewer", "admin"), false);
  });
});

describe("passwords", () => {
  it("hashes to the stored format", async () => {
    const hash = await hashPassword("correct-horse-battery");
    const [scheme, salt, digest] = hash.split("$");
    assert.equal(scheme, "scrypt");
    assert.equal(salt.length, 32); // 16 bytes hex
    assert.equal(digest.length, 128); // 64 bytes hex
  });

  it("salts, so the same password hashes differently every time", async () => {
    const [a, b] = await Promise.all([hashPassword("same"), hashPassword("same")]);
    assert.notEqual(a, b);
  });

  it("accepts the right password", async () => {
    await setUsers({ username: "dana", role: "reviewer", password: "right-password-here" });
    const result = await authenticate("dana", "right-password-here");
    assert.deepEqual(result, { username: "dana", name: "DANA", role: "reviewer" });
  });

  it("rejects the wrong password", async () => {
    await setUsers({ username: "dana", role: "reviewer", password: "right-password-here" });
    assert.equal(await authenticate("dana", "wrong-password-here"), null);
  });

  it("rejects an unknown user without revealing that it is unknown", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    assert.equal(await authenticate("nobody", "anything-at-all"), null);
  });

  it("spends comparable time on an unknown user as on a wrong password", async () => {
    // The decoy hash exists so this endpoint cannot be used to enumerate accounts. A loose
    // bound: an early `return null` on unknown users would be orders of magnitude faster,
    // not merely faster.
    await setUsers({ username: "dana", role: "reviewer", password: "right-password-here" });
    const time = async (user: string) => {
      const start = process.hrtime.bigint();
      await authenticate(user, "some-wrong-password");
      return Number(process.hrtime.bigint() - start) / 1e6;
    };
    const known = await time("dana");
    const unknown = await time("nobody");
    assert.ok(
      unknown > known / 10,
      `unknown user returned in ${unknown.toFixed(1)}ms vs ${known.toFixed(1)}ms for a known one`,
    );
  });

  it("rejects a stored hash that is not in the expected format", async () => {
    writeFileSync(
      usersFile,
      JSON.stringify([{ username: "dana", name: "D", role: "reviewer", password: "plaintext" }]),
    );
    assert.equal(await authenticate("dana", "plaintext"), null);
  });
});

describe("sessions", () => {
  it("round-trips", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    const s = session();
    assert.deepEqual(readSession(cookieFor(s)), s);
  });

  it("round-trips a name with separators and unicode in it", async () => {
    await setUsers({ username: "dana", role: "admin" });
    const s = { username: "dana", name: "Dana | O'Brien — ops", role: "admin" as Role };
    const token = issueSession(s);
    assert.ok(token);
    assert.deepEqual(readSession(token), s);
  });

  it("rejects a flipped role", async () => {
    // The exact attack: take a reviewer's cookie and rewrite the role to admin.
    await setUsers({ username: "dana", role: "reviewer" }, { username: "admin", role: "admin" });
    const tampered = cookieFor(session("dana", "reviewer")).replace("reviewer", "admin");
    assert.equal(readSession(tampered), null);
  });

  it("rejects a forged signature", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    const parts = cookieFor(session()).split("|");
    parts[4] = "x".repeat(parts[4].length);
    assert.equal(readSession(parts.join("|")), null);
  });

  it("rejects a token signed with a different secret", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    const token = cookieFor(session());
    process.env.BLUEPRINT_SESSION_SECRET = "a-completely-different-secret";
    assert.equal(readSession(token), null);
  });

  it("rejects an expired token", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    const parts = cookieFor(session()).split("|");
    parts[3] = String(Date.now() - 1000);
    assert.equal(readSession(parts.join("|")), null); // signature no longer covers it either
  });

  it("rejects malformed and empty cookies", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    for (const bad of ["", "a|b|c", "a|b|c|d|e|f", "notacookie", "|||"]) {
      assert.equal(readSession(bad), null, `accepted ${JSON.stringify(bad)}`);
    }
    assert.equal(readSession(undefined), null);
  });

  it("issues nothing when no secret is configured", () => {
    delete process.env.BLUEPRINT_SESSION_SECRET;
    assert.equal(issueSession(session()), null);
  });

  it("reads nothing when no secret is configured", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    const token = cookieFor(session());
    delete process.env.BLUEPRINT_SESSION_SECRET;
    assert.equal(readSession(token), null);
  });
});

describe("a session is only as valid as the account behind it", () => {
  it("rejects a session whose account was deleted", async () => {
    await setUsers({ username: "dana", role: "admin" });
    const token = cookieFor(session("dana", "admin"));
    assert.ok(readSession(token), "should be valid while the account exists");

    await setUsers(); // account removed
    assert.equal(readSession(token), null);
  });

  it("rejects a session whose account was demoted", async () => {
    await setUsers({ username: "dana", role: "admin" });
    const token = cookieFor(session("dana", "admin"));
    assert.ok(readSession(token));

    await setUsers({ username: "dana", role: "reviewer" }); // same account, lower role
    assert.equal(readSession(token), null, "an admin cookie survived a demotion");
  });

  it("rejects a session for a user who never existed", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    assert.equal(readSession(cookieFor(session("ghost", "reviewer"))), null);
  });

  it("picks up a change without a restart", async () => {
    // The cache is keyed on the file's mtime and size. Before that it loaded once and never
    // re-read, so deleting a user changed nothing until the process restarted.
    await setUsers({ username: "dana", role: "admin" });
    const token = cookieFor(session("dana", "admin"));
    assert.ok(readSession(token));

    await setUsers({ username: "dana", role: "reviewer" });
    assert.equal(readSession(token), null);

    await setUsers({ username: "dana", role: "admin" });
    assert.ok(readSession(token), "should be valid again once the role is restored");
  });
});

describe("isConfigured", () => {
  it("is false with no secret", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    delete process.env.BLUEPRINT_SESSION_SECRET;
    assert.equal(isConfigured(), false);
  });

  it("is false with no accounts", async () => {
    await setUsers();
    assert.equal(isConfigured(), false);
  });

  it("is false when the users file does not exist", () => {
    process.env.BLUEPRINT_USERS_FILE = join(dir, "nothing-here.json");
    assert.equal(isConfigured(), false);
  });

  it("is false when the users file is not valid JSON", () => {
    const broken = join(dir, "broken.json");
    writeFileSync(broken, "{ not json");
    process.env.BLUEPRINT_USERS_FILE = broken;
    assert.equal(isConfigured(), false);
  });

  it("is true with a secret and at least one account", async () => {
    await setUsers({ username: "dana", role: "reviewer" });
    assert.equal(isConfigured(), true);
  });
});
