# Demo script

A three-minute walkthrough. Written to be recorded from, and to be read on its own if nobody
ever records it.

The point of the ordering: **show the thing working before explaining how it works.** An
interviewer who sees a stakeholder get a real question from an agent in the first twenty
seconds will watch the rest. One who gets an architecture diagram first will not.

---

## Before you start

```bash
# terminal 1
uv run uvicorn blueprint.api:app_from_env --factory --port 8000

# terminal 2
cd frontend && npm run dev
```

Set `BLUEPRINT_STORE_PATH=runs/blueprint.db` so the admin view has the migrated history in it.
Sign in at <http://localhost:3000/login> **before recording** so no password is ever on screen.

Three traps that will bite you live, all of them already encountered:

- **Do not use `uvicorn --reload`.** On Windows it runs the app in a `multiprocessing` spawn
  child and the SDK cannot start its CLI from there. Every session create fails.
- **Password managers autofill the login form** and a working sign-in then looks broken. Use a
  private window, or sign in before you hit record.
- **Browser extensions** (Grammarly especially) used to trigger a hydration overlay on first
  load. Fixed with `suppressHydrationWarning`, but if you are on an older commit, that red box
  is the extension, not the app.

---

## 1 · The stakeholder (0:00 – 1:00)

Open <http://localhost:3000> and type something with a real problem in it, not a feature
request:

> We do about 30 new hires a month and a coordinator checks every packet by hand against the
> onboarding checklist. It takes 20–30 minutes each and things still slip through to payroll.

**What to point at while it streams:**

- The reply arrives token by token — that is real SSE from the SDK subprocess, not a spinner
  then a dump.
- The question that comes back is *specific*: it asks who depends on the work, not "tell me
  more". That is the canvas category driving it.
- Answer thinly on purpose — say **"just the HR team"** — and the agent asks a follow-up about
  who is waiting downstream. That follow-up came from a judge that ran *inside* the tool call
  that recorded the answer, so the gap and the question land in the same reply.

Say the line that matters: **the seven categories are fixed, everything inside them is not.**

## 2 · Hand it over (1:00 – 1:15)

You do not have ten minutes to complete a canvas on camera. Open a finished one instead:

<http://localhost:3000/s/32ddecd8-4f2e-4273-a2ab-5da3405a9e87>

Scroll to the bottom. The agent has summarised everything back and said a reviewer will look at
it. Note what the stakeholder **never** sees: no categories, no risk level, no classifier
output. That is a property of the response model, not a UI choice.

## 3 · The reviewer (1:15 – 2:15)

<http://localhost:3000/review>

- The queue is only completed conversations. Risk badges, open-gap markers, status.
- Open **"New hire packet completeness checker and requirements Q&A for HR Ops"**.
- **Why this was flagged** is the moment. `named integration`, ELEVATED, with the evidence
  quoted: *"ADP, ADP Workforce Now, BambooHR, sharepoint"*. Rules, not a model — so it is
  explainable, testable, and the reviewer can disagree with a specific phrase.
- Scroll the canvas. Point at `system_integrations v2` — that category was re-recorded after a
  send-back, and the version number says so.
- Show **send back** with a note. Then say what happens next: the stakeholder gets the question
  in the agent's own voice, never the reviewer's words. The privacy boundary is enforced in the
  transcript endpoint, and there is a test named after it.

## 4 · The admin (2:15 – 3:00)

<http://localhost:3000/admin>

- Every session including the unfinished ones, with total spend.
- Open the built one. The trace: classification **with its rationale and cost**, the canvas with
  versions, every turn with its tool calls and completeness verdicts, then the build report —
  six files, 379 lines, tests passed, $0.4594.
- Scroll to the **guard decisions** inside the build. Two commands were *denied* and the Builder
  adapted. That is the permission hook doing its job, recorded.
- Finish on the **evals** panel: the benchmark that chose the classifier, and the risk-gate suite
  at 0 false negatives.

**Closing line:** the SDK gave the agent loop. Everything that makes it safe to point at a
stakeholder — the canvas as state, the gate, the jail, the verification, the evals — is the
part worth talking about.

---

## If you have thirty seconds instead of three minutes

Open the admin trace of the built session and scroll once. It contains the whole story:
what was asked, what was matched and why, what was flagged and on what evidence, who approved
it, what got built, and what the guard refused.

## If they want to see the code

Three files, in this order:

1. `blueprint/builder.py` — a `PreToolUse` hook as the entire permission policy.
2. `blueprint/review.py` — risk as rules that carry their own evidence.
3. `tests/test_isolation.py` — a test that source-scans the package to prove a stakeholder's
   agent can never see developer context.

## Terminal-only fallback

If the frontend will not start, the whole pipeline still demos from a terminal:

```bash
uv run python cli.py chat --script conversations/hr_onboarding.txt
uv run python cli.py review runs/<id>.json
uv run python evals/risk_gate/run.py
```

The last one is free, takes a second, and prints a table.
