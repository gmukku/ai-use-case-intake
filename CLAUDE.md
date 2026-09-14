# CLAUDE.md — Blueprint AI

*Blueprint AI — an agentic AI system built on the Claude Agent SDK that turns a stakeholder's idea into a reviewed, working prototype.*

## What this project is

A non-technical stakeholder describes an idea for an AI or automation use case in plain conversation. The agent runs a structured discovery conversation, grounded in the GenAI Discovery Canvas I developed and refined through production use (reproduced in full below), adapting its follow-up questions to what the stakeholder actually says, and offering concrete, domain-relevant examples whenever asked. Once discovery is complete and a human has reviewed the compiled spec, a Builder subagent autonomously scaffolds a small, clearly-labeled illustrative prototype matching the requested output format, using the same file-writing and code-execution tooling that powers Claude Code itself.

This is a portfolio project for Forward Deployed Engineer interviews. It's intentionally not tied to any single employer's data. Everything in it, every document, every example request, is synthetic.

## How I want you to work with me

- I'm building this to learn, not just to ship. Before writing a piece of code, explain the concept and the decision behind it in plain terms first, then write it.
- Where a decision has real trade-offs, brainstorm it with me instead of silently picking one. Give me the options, what you'd lean toward, and why.
- Once I've shown I understand a concept, don't re-explain it every time it recurs, move faster on repeats of the same pattern.
- At natural checkpoints, hand me a small, well-scoped piece to write myself before you write the rest, and check what I wrote before moving on.
- When something breaks, walk me through how you're diagnosing it rather than just silently fixing it.
- Tie new concepts back to things I already know: n8n, Copilot Studio, the PrismHR MCP server, the GitHub Actions plus Claude API translation pipeline, human-in-the-loop design. I've built the equivalent of a lot of this before, just not in this stack.

## Where I'm starting from

- Strong already: Python, MCP server design, evals (reference-based plus human or LLM-judge validation), HITL design, cost and token consciousness, Claude API integration via GitHub Actions.
- New to me: the Claude Agent SDK specifically (I've done personal work with LangChain's DeepAgents, but no Agent SDK before this project), and all of JavaScript, React, and Next.js, genuinely zero prior exposure.
- Because of that: teach Claude Agent SDK fundamentals in the flow of building too, don't assume I already know its primitives (subagents, hooks, permissions, sessions) just because I'm comfortable with Python. Same goes for JS and React: teach the fundamentals as we build, not as a separate course first. Default every new component to a Client Component (`"use client"`) until I'm comfortable. Server Components are worth learning later, not on day one.

## The discovery canvas (the core content the agent runs on)

Seven categories, always covered in every conversation, in this order. These are the deterministic backbone, every conversation covers all seven, none get skipped. The specific follow-up questions and examples inside each one are where the model gets real latitude. That split, fixed categories with adaptive execution inside them, is deliberate. It's the same distinction that scored highest for me in a real interview, so build it in on purpose. Don't let me talk you into making everything either fully scripted or fully freeform.

1. **Key stakeholders** — Who will use or depend on this AI system?
2. **Key activities** — What's the existing process for this task? How many users perform it (e.g. a team of 2 vs a team of 12)? How often (daily, weekly, monthly, quarterly)?
3. **Value proposition** — What's the core problem the system solves (time savings, reduced errors, etc.)? Can we measure the impact?
4. **System integrations** — What other systems or third-party apps does this process touch (e.g. Salesforce, Prism, Mineral)?
5. **Input source** — Any external knowledge sources involved (SharePoint, Salesforce, a database, an external website)?
6. **Input types** (multi-select) — Input prompts, document uploads, images. Ask for real samples.
7. **Output format** (multi-select) — Summarization, comparison, workflow automation, drafting into a specific template, pulling data from a source, or a question-and-answer chatbot interface.

When the stakeholder asks for an example on any category (e.g. "what's an example of a system integration"), the agent should give 2 to 4 concrete, contextually relevant examples grounded in what's already been said, not a generic fixed list. See Department skills below for how those examples are sourced.

## Department skills

Four departments to start: HR, Finance, Sales, and Customer Success. Chosen as illustrative, not exhaustive, say so plainly in the README rather than implying full department coverage.

Each department gets its own `SKILL.md` under `skills/` at the repo root (not `.claude/skills/`: these are the app's runtime data, loaded by our own parser, not Claude Code configuration), holding that department's typical stakeholders and roles, typical systems and integrations (e.g. Finance: NetSuite, QuickBooks, Concur; Sales: Salesforce, HubSpot, Outreach; Customer Success: Zendesk, Intercom, Gainsight; HR: Workday, BambooHR, Greenhouse), typical input sources, and output-format patterns that tend to fit that department's work.

Two design decisions worth keeping deliberate, not accidental:

- **Matching is one-to-many, not one-to-one.** A request can plausibly touch several departments at once (e.g. "automate vendor onboarding" touches finance, IT, and legal). Design for 1 to 3 skills matching per conversation, don't force a single pick.
- **Selection is its own explicit step.** Something has to decide which skill(s) apply, most naturally a small classification pass over the stakeholder's early answers (who they are, what team, what the existing process is). Treat this as a real piece of the pipeline, similar in shape to the classification and anomaly-flagging work already done elsewhere, not something that happens for free.

This also resolves the earlier open question about where examples come from: department skills handle the static, procedural example banks (who, what systems, what outputs, per department). MCP still handles the SOP-document grounding, since that's retrieval over content that can change or grow, a genuinely different job from a fixed taxonomy. Both mechanisms stay, each doing the job it's naturally suited to.

## Architecture

- **Orchestrator** — the main Agent SDK loop.
- **Discovery subagent** — runs the canvas conversation, decides whether a category is sufficiently answered or needs a follow-up, and supplies contextual examples on request by drawing on whichever department skill(s) matched the conversation (see Department skills above). Its own taxonomy (the 7 canvas categories) and output-format templates should also live as project-level skills, not get re-prompted every turn.
- **Completeness check** — after each turn, decide whether to keep going, ask a clarifying question, or, once all 7 are sufficiently covered, hand off to the HITL gate. This is the same "confident enough to proceed?" decision from the earlier version of this project, just running per-category now instead of once.
- **HITL gate** — a human reviews the compiled discovery spec before the Builder touches anything, with extra scrutiny whenever a named system integration appears.
- **Builder subagent** — once approved, autonomously scaffolds a small illustrative prototype matching the requested output format, using the SDK's built-in file and code-execution tools. Scope this deliberately small and label it clearly as a proof of concept. This is not meant to become a general app generator.
- **Audit layer** — logs every step, every retrieved example, every human decision.
- **Three separate UI views, not one:**
  - *Requester view* — plain language only: the conversation, then the eventual outcome. No mention of classifiers, confidence scores, or retrieval.
  - *Reviewer view* — the approval queue, risk badges, approve or reject.
  - *Builder or admin view* — the full technical trace and the evals dashboard. This one is for me, not for whoever submitted the request.

## Tech stack

- Backend: Python, Claude Agent SDK (subagents, hooks, permissions, sessions), wrapped in FastAPI.
- Model access: Claude via Amazon Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`), with a direct Anthropic API key as the local fallback.
- Knowledge grounding: a small MCP server exposing synthetic SOP-style docs, and possibly a bank of domain examples for the canvas categories (see open decisions).
- Frontend: Next.js (App Router), TypeScript, Tailwind. Client Components by default until I'm ready for Server Components.
- Storage: SQLite.
- Everything synthetic, no real company data, ever.

## Evals

Benchmark of 15 to 20 example discovery conversations with human-authored "gold" outcomes. Measure: discovery completeness (were all 7 categories actually captured), example relevance (were suggested examples actually on-topic), a rubric-scored judge comparison of the compiled spec against the gold one (named dimensions: completeness, accuracy, actionability, never a vague overall quality score), and a false-negative check on the risk gate specifically, cases that should have been flagged for extra review but weren't. Log latency and token cost per run too.

## Open design decisions to make together, not unilaterally

- How small "small illustrative prototype" actually is for the Builder subagent. Decide this per output format as we build toward it, not all at once up front.

## Build order

1. Orchestrator plus Discovery subagent, hand-tested against a couple of sample conversations
2. Department skills for HR, Finance, Sales, and Customer Success, plus the skill-matching/selection step
3. MCP-connected SOP-document grounding
4. Completeness check and clarification loop
5. HITL gate logic
6. Builder subagent, starting with the simplest output format (e.g. a Q&A/chatbot stub) before attempting others
7. FastAPI endpoints wrapping all of the above
8. Next.js scaffold plus the requester view (this is also where most of my React learning happens, go slow here)
9. Reviewer view
10. Builder or admin view (trace and evals)
11. Eval harness
12. Audit logging
13. README, architecture diagram, short demo clip
