# Frontend

Next.js (App Router) serving three surfaces from one app, deliberately built from different
data rather than one API with a role flag:

| Route | Who | Shows |
| --- | --- | --- |
| `/s/[id]` | the requester | the conversation, and the outcome. No scores, no classifiers |
| `/review` | a reviewer | the queue, the compiled spec, risk flags, approve / send back / reject |
| `/admin` | you | the full technical trace and the eval dashboard |

`/review` and `/admin` are gated in `proxy.ts` — one matcher, one check, so a new protected
route is covered by existing code rather than by remembering.

## Running it

The backend must be up first (`uv run uvicorn blueprint.api:app_from_env --factory --port 8000`
from the repo root).

```bash
npm ci
npm run dev
```

## Configuration

Put these in `frontend/.env.local`, which is gitignored. **Sign-in fails closed**: with no
session secret or no accounts, every protected route is refused rather than opened.

| Variable | Default | What it does |
| --- | --- | --- |
| `BLUEPRINT_SESSION_SECRET` | *required* | HMAC key for the session cookie. Rotating it signs everyone out |
| `BLUEPRINT_USERS_FILE` | `users.json` | Account store. Never contains a plaintext password |
| `BLUEPRINT_API_URL` | `http://localhost:8000` | Backend, server-side |
| `BLUEPRINT_ADMIN_TOKEN` | unset | Bearer token for the backend's reviewer/admin routes |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend, browser-side (streaming) |
| `BLUEPRINT_LOGIN_MAX_PER_USER` | `10` | Failed sign-ins per username per window |
| `BLUEPRINT_LOGIN_MAX_PER_IP` | `30` | Failed sign-ins per address per window |
| `BLUEPRINT_LOGIN_WINDOW_S` | `900` | Length of that window, in seconds |

Generate a secret and add an account:

```bash
node -e "console.log(require('node:crypto').randomBytes(32).toString('hex'))"
```

```bash
npm run add-user
```

`add-user` prompts for a username, display name, role (`reviewer` or `admin`) and password,
and writes a scrypt hash. `users.json` is gitignored.

### About the sign-in throttle

Two independent budgets, because neither alone is enough: per-username catches a distributed
attack on one account, per-address catches one host spraying many usernames. Only failures
count, and a success clears the username's budget but deliberately not the address's — one
valid credential should not reset an attacker's whole allowance.

Two limits worth knowing before relying on it:

- **It is in-process.** Two Next instances means two budgets. That matches the API's
  single-process deployment; if either becomes multi-process this needs a shared store, not a
  smaller number.
- **`x-forwarded-for` is a request header.** Without a proxy that overwrites it, an attacker
  sets it per request and the per-address budget is free to bypass. That is why the
  per-username budget exists — treat per-address as defence in depth, not as the control.

## Checks

```bash
npm run typecheck && npx eslint . && npm run build && npm test
npm run test:e2e
```

`npm run typecheck` runs `next typegen` first: `PageProps`, `LayoutProps` and `RouteContext`
are generated into `.next/types`, which is gitignored, so a fresh checkout cannot typecheck
without generating them. All of these run in CI.

`test:e2e` drives the requester view in Chromium against `next start`, and **stubs the API in
the browser** — no backend, no API key, no spend. That is not a shortcut: a real turn is
non-deterministic, so there is nothing stable to assert on, and the cases worth testing (a
frame split across network chunks, a stream that dies mid-JSON) are ones a healthy server will
not produce on request. First run locally needs `npx playwright install chromium`.
