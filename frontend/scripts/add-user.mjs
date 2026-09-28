#!/usr/bin/env node
/**
 * Add or update a reviewer/admin account.
 *
 *   npm run add-user -- <username> <role: reviewer|admin> "<Display Name>"
 *
 * Prompts for the password with echo off, hashes it with scrypt, and writes the entry to
 * `users.json` (gitignored). The plaintext is never written anywhere, never passed as an
 * argument (where it would land in shell history), and never printed.
 *
 * Also generates BLUEPRINT_SESSION_SECRET into .env.local the first time, since sign-in
 * fails closed without one.
 */

import { randomBytes, scrypt } from "node:crypto";
import { appendFileSync, existsSync, readFileSync, writeFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { promisify } from "node:util";

const scryptAsync = promisify(scrypt);
const USERS_FILE = process.env.BLUEPRINT_USERS_FILE ?? "users.json";
const ENV_FILE = ".env.local";

function usage(message) {
  console.error(`${message}\n\n  npm run add-user -- <username> <reviewer|admin> "<Display Name>"`);
  process.exit(1);
}

/** Read a line with the terminal echo turned off, so the password never appears on screen. */
function askHidden(prompt) {
  return new Promise((resolve) => {
    const rl = createInterface({ input: process.stdin, output: process.stdout, terminal: true });
    const onData = (char) => {
      // Repaint the prompt without the typed characters.
      if (![`\n`, `\r`, `\u0004`].includes(char.toString())) {
        process.stdout.clearLine(0);
        process.stdout.cursorTo(0);
        process.stdout.write(prompt);
      }
    };
    process.stdin.on("data", onData);
    rl.question(prompt, (answer) => {
      process.stdin.off("data", onData);
      rl.close();
      process.stdout.write("\n");
      resolve(answer);
    });
  });
}

async function main() {
  const [username, role, name] = process.argv.slice(2);
  if (!username) usage("A username is required.");
  if (role !== "reviewer" && role !== "admin") usage("Role must be 'reviewer' or 'admin'.");
  if (!name) usage("A display name is required — it is what appears in the audit log.");

  const password = await askHidden(`Password for ${username}: `);
  if (password.length < 12) {
    console.error("\nUse at least 12 characters. This guards the full session trace.");
    process.exit(1);
  }
  const again = await askHidden("Again: ");
  if (password !== again) {
    console.error("\nThose did not match.");
    process.exit(1);
  }

  const salt = randomBytes(16);
  const derived = await scryptAsync(password, salt, 64);
  const hashed = `scrypt$${salt.toString("hex")}$${derived.toString("hex")}`;

  const users = existsSync(USERS_FILE) ? JSON.parse(readFileSync(USERS_FILE, "utf8")) : [];
  const existing = users.findIndex((u) => u.username === username);
  const entry = { username, name, role, password: hashed };
  if (existing >= 0) {
    users[existing] = entry;
  } else {
    users.push(entry);
  }
  writeFileSync(USERS_FILE, JSON.stringify(users, null, 2) + "\n", { mode: 0o600 });
  console.log(`\n${existing >= 0 ? "Updated" : "Added"} ${username} (${role}) in ${USERS_FILE}`);

  const env = existsSync(ENV_FILE) ? readFileSync(ENV_FILE, "utf8") : "";
  if (!/^BLUEPRINT_SESSION_SECRET=.+/m.test(env)) {
    const secret = randomBytes(32).toString("base64url");
    appendFileSync(ENV_FILE, `${env.endsWith("\n") || !env ? "" : "\n"}BLUEPRINT_SESSION_SECRET=${secret}\n`);
    console.log(`Generated BLUEPRINT_SESSION_SECRET into ${ENV_FILE}.`);
    console.log("Restart `npm run dev` for it to take effect.");
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
