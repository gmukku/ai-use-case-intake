import { defineConfig, devices } from "@playwright/test";

/**
 * End-to-end tests for the requester view.
 *
 * **The backend is always stubbed.** Every test routes the API in the browser and never
 * reaches a real server, for three reasons that all matter: a real turn costs money and needs
 * the user's key, model output is non-deterministic so there is nothing stable to assert on,
 * and the interesting cases here — a frame split across chunks, a stream that dies halfway —
 * are ones a healthy server will not produce on demand. Stubbing is not a compromise; it is
 * the only way to test them.
 *
 * What this covers that the unit tests cannot: the SSE reader against a real browser stream,
 * React state across a real turn, and geometry — a sticky composer once covered the reopen
 * button completely, which no assertion about state would have caught.
 */

const PORT = Number(process.env.E2E_PORT ?? 3100);

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0, // a flaky e2e test is a bug report, not something to paper over with a retry
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    // The production build, not `next dev`: dev recompiles on first hit, which turns the
    // first test's timeout into a build timer. It also means the tests exercise the same
    // output that ships.
    command: `npm run start -- --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: {
      // Protected routes are refused without these; the requester view needs neither, and
      // setting them keeps a misconfiguration from being mistaken for a UI failure.
      BLUEPRINT_SESSION_SECRET: "e2e-not-a-real-secret",
      BLUEPRINT_USERS_FILE: "e2e-users-that-do-not-exist.json",
    },
  },
});
