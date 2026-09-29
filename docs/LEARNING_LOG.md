# Learning log

A running record of what was learned building Blueprint AI, and how the project progressed.
Written for the builder's own retention first; readable by anyone evaluating the project.

Conventions:
- **Progress** is a timeline by build-order step, with commit hashes so a reader can diff.
- **Concepts** are one entry each: what it is, how this project uses it, the gotcha, and where
  the code is. Entries are added the first time a concept is *used*, not just mentioned.
- **Decisions** record the options considered and why one won, so the choice is not
  re-litigated later without new information.
- **Mistakes** are kept, not cleaned up. Each one names what it taught.

Updated at every checkpoint (end of a build-order step, or any time a mistake taught something).

---

## Progress

| Date | Step | What landed | Commit |
|---|---|---|---|
| 2026-09-12 | 0 | Environment: uv project, Python 3.12 pinned, `.gitignore` before any key existed, `.env.example` with both auth paths. Two SDK smoke scripts (`examples/`). | — |
| 2026-09-12 | 1 | Canvas backbone, discovery agent with `record_canvas_answer` tool, `DiscoverySession` orchestrator with streaming turn loop, JSON-lines logging, CLI with replayable scripts. First live 10-turn HR conversation completes the canvas for ~$0.15. | `234af83` |
| 2026-09-13 | 1 | `.gitattributes` pins LF line endings. | `e07752c` |
| 2026-09-13 | 2a | Four department `SKILL.md` files, strict loader. | `809f740` |
| 2026-09-13 | 2b | Structured-output department classifier, `department_examples` tool, orchestrator runs the classifier once `key_activities` is captured. | `dbbd0a9` |
| 2026-09-13 | 2 | Skills moved from `.claude/skills/` to `skills/`; path resolved from the package, not the CWD. | `9163007` |
| 2026-09-14 | 2 | Classifier benchmark: 20 labeled cases, Opus vs Sonnet × 3 repeats. Opus kept on stability. One label corrected by the evidence. | `368e448` |
| 2026-09-14 | — | Hardening: SDK error containment, idle timeout, input validation, validated settings, pre-commit + CI with secret scanning. | `7504eda` |
| 2026-09-14 | — | Project gitleaks rules after the default rule missed a plausible key shape. | `752ca82` |
| 2026-09-14 | — | This learning log, and the CLAUDE.md rule to keep it current. | `983489d` |
| 2026-09-14 | — | Scoped web search (Tavily) as an in-process tool with a domain allowlist, plus a `PreToolUse` hook capping calls per session. First hook. Live run: one search, generic query, USCIS-only results, cited in the recorded summary. | `08925a6` |

| 2026-09-14 | 3 | SOP library as an external stdio MCP server: eight synthetic SOPs, strict loader, IDF-weighted keyword search, `MCPServer` over stdio, wired via the SDK's `mcp_servers` stdio config next to the in-process canvas server. Live run: on "our onboarding checklist" the agent searched and read the packet-review SOP on turn 1, then asked whether the I-9 document inspection is part of the checklist pass. | `6101b39` |

| 2026-09-14 | 4 | Completeness check and clarification loop: per-category rubric as data, a structured-output judge run inside the record tool so a gap becomes a follow-up in the same reply, a clarification budget of two per category, `is_ready_for_review` as the handoff decision, open gaps carried to the reviewer. Live run: "just the HR team" was flagged for dependents, the agent asked, the re-record passed. | `fb20a7c` |

| 2026-09-14 | 5 | HITL gate: compiled spec from the snapshot, rule-based risk with evidence per flag, append-only review log with validated decisions, send-back delivered through the SDK's `resume=` as a reviewer-framed instruction turn. CLI grew `review` and `resume`. Live round trip: send-back → resume → the agent asked for the intake system's product name → re-recorded as v2 → spec v2. | `fbb9c5c` |

| 2026-09-15 | 6 | Builder subagent: parent + `AgentDefinition` via the `Agent` tool, a `PreToolUse` hook as the entire permission policy (workspace jail, command allowlist, parent denied), Python-side verification, the first output-format template (Q&A stub). Live build: six files, 8 tests passed, two commands denied and adapted, $0.46. Found and closed an auto-memory leak into every agent. | `8bdd6c7` |

| 2026-09-28 | 7 | FastAPI over the whole pipeline: three route groups for the three audiences, SSE for the requester's turn, a `RunStore` behind an interface, one-turn-at-a-time session locking, an idle sweeper, bearer token on reviewer/admin, builds as background tasks. Live: a real turn streamed end to end, snapshot persisted under the API's own uuid, queue picking up earlier CLI runs from the same `runs/`. | `f4aa98e` |

| 2026-09-28 | 8a | Next.js 16 scaffold and the requester's conversation view: a typed API client that reads the SSE stream by hand, streaming text with a caret, lazy session creation. Live: two turns streamed into the browser. | `7185af4` |
| 2026-09-28 | 8b | `/s/<id>` routing with an in-place URL rewrite, transcript restored from the server, reopen after a send-back, and 👍/👎 feedback (new `blueprint/feedback.py`, two new endpoints). Live: the whole send-back → reopen → answer → re-record loop through the browser. Three bugs found by running it that neither the tests nor the typechecker could see. | `6391f50` |

| 2026-09-28 | 9 | Reviewer view: approval queue with risk badges, the compiled spec with evidence per flag, and approve / send back / reject. The frontend grows a server side — route handlers hold the admin token, `server-only` makes that a build error to get wrong, and reviewer identity moves into an httpOnly cookie. | `c161367` |

| 2026-09-28 | 10 | Admin view: every session including unfinished ones, the full trace behind each (classification, canvas versions, per-turn tool calls and verdicts, denied searches, feedback, decisions, build report), build controls behind the gate, and the recorded eval runs as a table. New `blueprint/eval_runs.py` reads what the harness writes. | `1edca8d` |

| 2026-09-28 | 11 | Shared eval harness plus two suites. `risk_gate`: 18 labeled canvases, deterministic, free — found three flags that were not firing at all. `completeness`: 21 labeled captures across three models, settling the checker-model question deferred since step 4. Both appear in the dashboard without it changing. | `141be0d` |

| 2026-09-28 | — | Real sign-in in front of `/review` and `/admin`: scrypt passwords, HMAC session cookies, roles, and `proxy.ts` as the single gate. Fails closed. Closes the deferred auth gap from steps 9 and 10. | `da59411` |
| 2026-09-28 | 12 | SQLite behind the existing `RunStore` protocol, an append-only `audit_events` table, redaction at write, and retention. 12 runs migrated with 96 derived events. No route changed. | `c102d8b` |

| 2026-09-28 | 13 | README with a verified Mermaid diagram and three real screenshots, an honest-limits section, and `docs/DEMO.md` as a three-minute walkthrough. | `59f0c1b` |

**All 13 build-order steps are complete.** 434 tests, ruff + mypy `--strict` clean, 36 commits.
~5.9k lines of backend, ~4.6k of tests, ~2.5k of frontend, ~0.9k of evals. Twelve recorded
conversations cost $1.08 in total.

What is *not* done is listed under Deferred and open below, and in the README's "Honest
limits". The largest are the two missing eval measures (they need a stakeholder simulator,
~$6–8 a run), the untested Bedrock path, and the requester view having no automated tests.

---

## Claude Agent SDK concepts

### The SDK is Claude Code as a subprocess
**What:** `claude-agent-sdk` bundles the Claude Code CLI (the wheel is platform-specific for
this reason) and spawns it as a child process, speaking JSON lines over stdio. Python is a
*client*; the agent loop, tool execution and context management live in the subprocess.
**Here:** every `DiscoverySession` is one subprocess. **Gotcha:** the subprocess reads
credentials from the environment at spawn time, so `load_dotenv()` must run before the first
call. **Contrast:** DeepAgents runs the graph in-process; Copilot Studio is the closer analogy
(configure a runtime you do not own). *`examples/hello_sdk.py`*

### `query()` vs `ClaudeSDKClient`
**What:** `query()` is a one-shot async generator; `ClaudeSDKClient` is a persistent connection
with `connect()` / `query()` / `receive_response()` / `disconnect()`. **Here:** the discovery
conversation uses the client (multi-turn); the classifier uses `query()` (pure function).
**Gotcha:** the client's `.query()` only *sends*; messages come back from `receive_response()`,
which yields until the turn's `ResultMessage` and then stops. That turn boundary is where the
orchestrator regains control. *`blueprint/orchestrator.py`, `blueprint/matching.py`*

### Message and content-block types
**What:** `SystemMessage` (init), `AssistantMessage` (list of `TextBlock` / `ToolUseBlock` /
`ThinkingBlock`), `UserMessage` (tool results), `ResultMessage` (cost, usage, session id).
**Gotcha:** an assistant turn is a *list of blocks*, never a string; always iterate and check
types. **Gotcha 2:** `ResultMessage.total_cost_usd` is a **running total** for the session, not
a per-turn cost. Per-turn cost is the delta. *`orchestrator.py` `stream()`*

### `setting_sources` and isolation
**What:** controls which filesystem settings the subprocess loads. The default (`None`) loads
`~/.claude` settings **and** `CLAUDE.md`. `[]` is isolation mode. **Here:** every agent passes
`setting_sources=[]`; the hello-world quoted CLAUDE.md back before this was set. **Why it
matters:** CLAUDE.md is instructions for building the project; a stakeholder's agent must
never see it. *`blueprint/discovery.py` `build_discovery_options`*

### `tools` vs `allowed_tools`
**What:** `tools=[]` removes the built-in tools (Read, Bash, …) from the model's context
entirely. `allowed_tools=[...]` only pre-approves tools so they run without a permission
prompt. **Gotcha:** I first assumed `allowed_tools=[]` stripped the tool schemas; it does not.
Both are set on the discovery options: `tools=[]` plus `allowed_tools` naming our MCP tools.
**Cost lesson:** the default Claude Code system prompt plus full tool list cost ~$0.14 for
"say hello"; a string `system_prompt` and `tools=[]` bring a discovery turn to ~1.5¢.

### In-process MCP tools
**What:** `@tool(name, description, schema)` on an async function plus
`create_sdk_mcp_server(name, tools=[...])`, registered on `mcp_servers`. Runs inside the
Python process; the model sees it as `mcp__<server>__<tool>`. **Here:** `record_canvas_answer`
and `department_examples`. **Key property:** arguments are validated against the JSON schema
*before* the handler runs, so an `enum` on `category` makes the seven canvas categories a hard
constraint at the schema layer. A handler returns `{"content": [...]}` and may set
`"is_error": True` to hand the model an actionable message. **Testing:** the in-process server
exposes `get_request_handler("tools/call")`, so tests dispatch through the real MCP layer and
get schema validation for free. *`blueprint/discovery.py`, `tests/test_orchestrator.py`*

### External MCP servers over stdio
**What:** the same `mcp_servers` dict that holds in-process servers also takes
`{"type": "stdio", "command": ..., "args": [...], "env": {...}}`. The CLI subprocess spawns
the server, does the MCP handshake, and exposes its tools as `mcp__<server>__<tool>`, so
`allowed_tools` and the trace treat them identically to in-process tools. **Here:** the SOP
library, launched as `<venv python> -m blueprint.sop_server` with `PYTHONPATH` set so the
package imports from any cwd. This is the client side of the PrismHR-server pattern.
**Gotchas:** (1) `mcp` 2.x renamed `FastMCP` to `MCPServer`; a v1-era prior would have
failed on import. (2) A stdio server must never write to stdout: it is the transport; log
to stderr. (3) Test the server with the `mcp` client (`stdio_client` + `ClientSession`)
before wiring it into the SDK, so a wiring failure is a config problem, not a code problem.
*`blueprint/sop_server.py`, `blueprint/discovery.py` `sop_server_config`, `tests/test_sops.py`*

### Streaming: `include_partial_messages`
**What:** opt-in; forwards the raw API stream events as `StreamEvent` objects, interleaved
*before* the complete `AssistantMessage` they belong to. **Here:** translated into our own
`TextDelta` / `ToolCallStarted` / `TurnCompleted` events so FastAPI and the frontend never
depend on the SDK wire format. **Rule:** build state from the complete message, use deltas
only for display. *`orchestrator.py` `_translate_stream_event`*

### Structured output: `output_format`
**What:** `output_format={"type": "json_schema", "schema": {...}}` on the options; the parsed
object arrives on `ResultMessage.structured_output`. **Here:** the classifier's 1–3 rule and
the allowed department set live in the schema, not in prose. Output is re-validated on receipt
(trust but verify, even upstream). **Gotcha:** structured output uses a hidden tool round-trip,
so `max_turns=1` fails intermittently; use 3. *`blueprint/matching.py`*

### `effort`
**What:** `effort="low" | "medium" | "high" | "xhigh" | "max"`; same model, less or more
thinking. **Here:** `low` for the classifier, a small well-specified job. Three-case probe and
the 120-call benchmark confirmed it is plenty.

### Cost and turn caps
**What:** `max_budget_usd` is a hard cap per session; `max_turns` caps agentic turns per
query (a tool round-trip counts as a turn, so leave headroom). **Here:** budget from settings
(default $3), `max_turns=8` for discovery.

### Error semantics: the SDK *raises* on error results
**What:** when the CLI emits a result with `is_error: true` (budget exceeded, max turns, API
failure) it exits non-zero, and the SDK surfaces that as a `ResultError` exception with
`subtype`, `terminal_reason`, `result`. The subprocess is gone afterwards. **Here:** `stream()`
catches `ClaudeSDKError` into an error `TurnResult`, marks `session.failure`, drops the client.
**Gotcha:** I first wrote the code assuming an error `ResultMessage` would be *yielded*; that
path was effectively unreachable. Reading the SDK source found it. *`orchestrator.py`*

### `RateLimitEvent`
**What:** emitted when rate-limit status changes (`allowed_warning`, `rejected`). **Here:**
logged at WARNING with utilization; not fatal.

### Subagents: `AgentDefinition` + the `Agent` tool
**What:** `AgentDefinition(description, prompt, tools, model)` registered under
`options.agents`; the parent invokes it through the `Agent` tool (the SDK docstring still says
`Task`; `tools=["Task"]` is accepted), it runs in a fresh context to completion and returns
one result. **Here:** the Builder. **Gotchas learned by probe:** (1) a subagent can only use
tools the *session* carries, so the parent's `tools` must list Read/Write/Bash even though the
parent must never use them; a hook denies the parent by `agent_type`. (2) `PreToolUse` hooks
fire inside subagents with `agent_type` set. (3) `permission_mode="acceptEdits"` covers file
edits only; Bash still prompts unless a hook returns `permissionDecision: "allow"`. (4) A
subagent cannot converse with a human, which is why discovery is the main session.
*`blueprint/builder.py`*

### Permissions: a hook as the whole policy
**What:** the SDK has three permission layers: `tools` (the universe), `permission_mode`
(what is auto-approved), and per-call decisions via `can_use_tool` (interactive) or a
`PreToolUse` hook (policy). **Here:** the hook is the policy. It *allows* vetted calls
explicitly (so the CLI never prompts) and *denies* the rest with a reason the model reads:
parent gets no file/shell tools; the builder is jailed to its workspace for reads and writes
(reads too, or `.env` walks into context); Bash is one operator-free command from a short
allowlist with no absolute paths outside the workspace. Every decision is recorded.
`can_use_tool` is deliberately unused: it asks a human per call, the wrong shape for an
autonomous job. **Verification is Python's, not the model's:** `pytest` in the workspace with
secrets stripped from the environment, plus file, line, and banner checks.

### Auto-memory is a third isolation channel
**What:** Claude Code injects the developer's per-project memory into any session whose
`cwd` maps to a known project, and a user-level index otherwise, regardless of
`setting_sources`. **Found:** the Builder's report quoted this project's memory notes
("standing standards on uv/ruff/mypy/event logs"). **Measured** with
`ClaudeSDKClient.get_context_usage()["memoryFiles"]`: 1 file (170 tokens) by default, 0 with
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` in the subprocess `env`. Every agent now passes it, and
`tests/test_isolation.py` scans the package source so no `ClaudeAgentOptions` can omit it.
The discovery agent had been carrying those notes silently since the memory files were
created. *`blueprint/isolation.py`*

### Nested `query()` from inside a tool handler
**What:** an in-process MCP tool handler runs inside the client's event loop while a session
turn is in flight; calling `query()` from it spawns a second, independent CLI subprocess.
**Verified** with a probe before building on it: ~3 s, no deadlock, outer session unaffected.
**Here:** the completeness checker runs inside `record_canvas_answer`. **Cost of the pattern:**
the check's latency lands before the model's text, so recording turns are a few seconds
slower to start streaming. *`blueprint/orchestrator.py` `_assess`*

### Hooks: `PreToolUse`
**What:** Python callbacks on lifecycle events (`PreToolUse`, `PostToolUse`, `Stop`,
`SubagentStart`, …), registered as `hooks={"PreToolUse": [HookMatcher(matcher=<tool name>,
hooks=[fn])]}`. The callback signature is `async (input, tool_use_id, context)`; for
`PreToolUse`, `input["tool_name"]` and `input["tool_input"]` are available, and returning
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
"permissionDecisionReason": "..."}}` blocks the call and shows the reason to the model.
Returning `{}` allows. **Here:** `WebSearchGuard.hook` caps `web_search` at N per session and
records every attempt. The tool never knows the guard exists; that separation is the point,
the `beforeEach` of the agent loop. **Gotcha:** the return type is the SDK's `HookJSONOutput`
TypedDict, not a bare dict; mypy enforces the shape. **Next use:** gating the Builder's file
writes. *`blueprint/websearch.py`*

### Sessions: `resume=`
**What:** the CLI persists each session's transcript on disk; `ClaudeAgentOptions(resume=
session_id)` reconnects a *new* subprocess to it with full context. Our options (custom
prompt, both MCP servers, hooks) are supplied again on reconnect; the model's memory comes
from the transcript, ours from the snapshot. `fork_session` / `resume_session_at` branch from
an earlier point (not used yet). **Here:** `DiscoverySession(snapshot=...)` rebuilds every
Python-side log *before* building the tools (the record tool closes over the state object),
then sets `resume`. **Gotchas:** (1) the SDK's running cost total restarts at zero in the new
subprocess: carry the earlier spend as an offset or per-turn cost goes negative (it did, in
the first live round trip). (2) `resume` is a *process* boundary as well as a time boundary;
anything not in the snapshot is gone. *`blueprint/orchestrator.py` `_restore`*

### Instruction turns: a reviewer speaks through the model
**What:** the SDK conversation has only user and assistant turns, so a message from someone
other than the stakeholder must be *framed* ("REVIEWER NOTE (the stakeholder cannot see
this): … ask them about this in your own words; do not mention a reviewer"). The model's reply
is what the stakeholder sees. **Here:** `send_reviewer_note`; the trace stores the raw note
with `origin="reviewer"` so the requester view can hide it. Same mechanism Claude Code uses
for its own system reminders. Verified in a probe before building on it.

---

## Engineering patterns and decisions

### Option A: discovery is the main session
**Options:** (A) discovery = main `ClaudeSDKClient` session, Python orchestrator around it;
(B) orchestrator agent delegates each turn to a discovery subagent; (C) orchestrator talks to
the stakeholder, subagents do side-jobs. **Chosen:** A with C's use of subagents for side-jobs.
**Why:** subagents cannot converse; A keeps the deterministic parts (which category, when to
hand off) in testable Python and gives the model latitude inside the conversation, which is
the fixed-backbone/adaptive-execution split expressed in code.

### The model reports state through a tool, not through text
**Options:** parse markers out of free text; a `record_canvas_answer` tool; structured output
on every turn. **Chosen:** the tool. **Why:** the deterministic/adaptive split lives in the
type system (the schema's `enum`), every capture is an explicit auditable event, and the
completeness check becomes a plain Python question over state.

### Append-only event logs
`CanvasState` is a list of frozen `CanvasEntry`s with turn numbers and UTC timestamps.
"Current answer" is derived (latest entry), never stored, so state and history cannot drift.
The audit layer becomes a query over data that already exists. Same shape for `TurnResult`s.

### Injected callables instead of shared globals
The record tool reads the turn number through `lambda: self.turn`; the examples tool reads
matched skills through `session.matched_skills`. Both are consulted at call time, so two
sessions never share state and the model cannot misattribute a capture (the schema forbids it
sending a turn number at all).

### Department skills: files we load, served via a tool (route B)
**Options:** (A) SDK-native `skills=[...]` with `.claude/skills/`; (B) standard `SKILL.md`
files parsed by us and served through `department_examples`. **Chosen:** B. **Why:** matches
can change mid-conversation without reconnecting, the session stays isolated from CLAUDE.md,
and every retrieval is a logged tool call. SDK-native skills are saved for the Builder, whose
skill set is known up front.

### Selection is an explicit classification pass
One-shot structured-output call once `key_activities` is captured (fallback: turn 3), result
stored on the session with its rationale. The discovery model never picks a department; it
asks for examples and gets an answer scoped by a decision it did not make.

### Runtime data does not live in a tool's dotfolder
`skills/` at the repo root, not `.claude/skills/`: application data, not Claude Code
configuration, and it keeps the coding assistant from discovering the departments as its own
skills. Paths resolve from `__file__`, with an env override, so the app works from any CWD.

### YAML for files humans author, JSON for files programs exchange
Eval cases in YAML (multi-line transcripts, comments, easy editing). Run snapshots, eval
results, and wire formats in JSON.

### Validate at every boundary
Model → tool: JSON schema plus handler checks with messages the model can act on. Env →
program: `settings.py` fails at startup naming the variable. Stakeholder → model: blank
rejected, length capped, before anything is billed.

### Web search: route B, scoped to regulatory grounding
**Options:** (A) Tavily's official MCP server via `npx` (stdio, no code, model sees the full
parameter surface); (B) our own in-process tool over Tavily's REST API. **Chosen:** B.
**Why:** the safety properties (domain allowlist, result cap, snippet trim, key handling, per-
session cap) belong in code, not prose. Scope is deliberately narrow: search sharpens a
follow-up, never answers the stakeholder; queries must be generic; off by default and always
off in evals so gold outcomes stay reproducible. The one-line rule that carries the design:
*grounding for questions, not answers.*

### Two grounding mechanisms, two jobs
Department skills are a fixed taxonomy served as *examples* (who, what systems, what outputs);
SOPs are *content* that changes and grows, served as *lookups*. Skills are loaded once and
selected by the classifier; SOPs are searched on demand when the stakeholder references an
existing process. Both feed the same behavior, sharper follow-ups, and both are cited in the
recorded summary so the reviewer can see what informed a capture. The stakeholder remains the
authority on what actually happens; a document is a prompt for a better question, not truth.

### Deterministic retrieval where the corpus is small
Eight documents do not justify embeddings. Keyword scoring with field weights (title >
summary > body), IDF term weighting so rare terms dominate generic ones, a phrase bonus, and
id tie-breaks give a ranker whose results are reproducible in tests and evals. The first
version without IDF ranked a QBR SOP above the I-9 SOP for a query full of "business days";
the fix was in the ranker, not the test.

### An independent judge, run inside the tool call
**Options for who judges sufficiency:** the discovery model itself (self-grading, nothing to
eval), a separate structured-output pass (independent, auditable, benchmarkable), or rules in
code (brittle on prose). **Chosen:** the separate pass, with the rubric as data in `canvas.py`
so the judge can only report gaps we defined. **When:** on each capture, not every turn and
not only at the end. **How the gap reaches the stakeholder:** through the record tool's own
response ("Incomplete: missing frequency. Ask something like: …"), so the follow-up lands in
the same reply with no injection and no extra tool. A per-category budget of two follow-ups
enforces "do not interrogate"; leftover gaps go to the reviewer instead of the stakeholder.
Cost: ~1.8¢ per verdict, ~35% of a conversation; a settings flag and a model parameter, so it
can be benchmarked down exactly like the classifier.

### A judge that sees only the summary needs a "re-record, don't re-ask" rule
The checker judges the recorded summary, not the transcript, by design (that is what the
reviewer will read). So when the model omits a detail it already heard, the checker flags it
and the naive response is to ask the stakeholder again. The prompt rule is: if the detail was
already given, re-record with it; otherwise ask once. Found on the first live run.

### Risk as rules with evidence, not a score
**Options:** a model-produced risk score (opaque; what CLAUDE.md warns against) or rules in
code, each flag carrying the matched text. **Chosen:** rules. `named_integration` is seeded
from the department skills' own system lists, so the taxonomy does double duty;
`writes_to_system` is negation-aware because every HR run's summary says "out of scope:
writing into the HRIS" and must *not* fire; overall level is the highest flag. The reviewer
sees "matched 'ADP' in system_integrations", and the false-negative eval (step 11) has
something concrete to measure. A model opinion can be added later as one more flag.

### The gate works on snapshots, not live sessions
Review happens hours after the conversation, so `compile_spec` and `assess_risk` take the
`to_dict()` snapshot and nothing else. Same code path for a run loaded from disk or from the
audit store, and pure data in/out for tests. Every snapshot-carried type got a `from_dict`
inverse; the snapshot is now the session's persistence format in all but name.

### How small the prototype is, for the Q&A format
README + 2–3 synthetic knowledge docs + `qa.py` (keyword retrieval, one model call, cited
answer) + `test_qa.py` (retrieval only, no key). Max 8 files, ~450 lines, `anthropic` the only
dependency, the prototype banner on every source file. Decided for this one format, as
CLAUDE.md asked; other formats raise `UnsupportedOutputFormatError` rather than guess.

### Output-format templates: files we load, not SDK-native skills
The SDK discovers skills from `<cwd>/.claude/skills/`; the Builder's cwd is the prototype
workspace, so SDK-native loading would mean copying the template into every workspace at
build time. Contrived. The template is a `SKILL.md` under `builder_skills/`, parsed by the
same frontmatter helper as department skills and placed in the subagent's prompt. The
SDK-native path was considered and documented here; it never earned its complexity.

### Classifier model chosen by benchmark, not by default
20 labeled cases × 3 repeats. Opus 95% exact / 0 unstable; Sonnet 91.7% / 1 unstable. Kept
Opus because *stability* is the property that matters for a once-per-conversation decision
that determines which examples a stakeholder sees; the cost delta is ~0.4¢ per conversation.
The labels were the judge (reference-based eval); an LLM judge is a different role, reserved
for outputs with no crisp reference (spec quality, example relevance).

### Fake the SDK at the boundary, exercise everything else for real
`FakeClient` replays scripted turns and emits `StreamEvent`s in the exact wire shape, but
dispatches tool calls through the *real* in-process MCP server. `FakeMatcher` keeps the
classifier out of unit tests. Unit tests never call the model; CI has no API key by design.

### The surface *is* the permission
CLAUDE.md asks for three views. The tempting implementation is one API with a role flag on
the caller; the implementation here is three route groups whose *responses are built from
different data*. `/sessions/{id}` assembles a `SessionStatus` that has no field for a
category, a risk level, or a classifier result, so there is no flag to get wrong and no
serializer to forget: a requester cannot be shown risk because the model they receive cannot
carry it. The bearer token on `/reviews` and `/admin` is the second layer, not the first.
*`blueprint/api.py` `SessionStatus`, `QueueItem`*

### SSE, not WebSockets
A discovery turn is one-way streaming: the stakeholder sends a message over plain POST, then
listens. Server-sent events are one HTTP response with `text/event-stream`, so they keep the
request/response model, work through proxies, and reconnect on their own; a WebSocket would
add a second protocol for a channel that never needs to talk back mid-turn. Named events
(`text`, `activity`, `done`, `error`) give the frontend a vocabulary that is ours, one more
layer away from the SDK's wire format. **Gotcha:** buffering. `X-Accel-Buffering: no` and
`Cache-Control: no-cache` are what stop a proxy from holding the whole turn and delivering it
as one block, which looks exactly like the agent being slow.
*`blueprint/api.py` `_sse`, `_stream_turn`*

### One turn at a time, per session
Each session owns an SDK subprocess, and a subprocess has one conversation. Two overlapping
turns would interleave into the same transcript, so each live session carries an
`asyncio.Lock`: the route acquires it and the SSE generator releases it in `finally`, and a
second turn gets `409` rather than a queue. Refusing is better than queueing here, because
the requester is a human who should be told their last message is still running.
*`blueprint/api.py` `LiveSession`, `send_message`*

### The run store is an interface from the first day it has one caller
`FileRunStore` writes the same `runs/<id>.json` the CLI has always written, but the API
depends on the `RunStore` Protocol, not on files. Step 12 swaps SQLite in behind it without
touching a route. The cost of the abstraction was about thirty lines; the cost of not having
it would have been a step-12 rewrite of every handler. The id is validated against a
UUID-shaped pattern *before* it becomes a path, so a traversal attempt never reaches the
filesystem. *`blueprint/store.py`*

### Everything with a side effect is injectable, again
`create_app` takes `session_factory`, `store`, `build_fn`, `skills` and `prototypes_dir`. The
whole API test suite runs against the real FastAPI app over a real ASGI transport with no
subprocess, no network and no disk — the same pattern as the orchestrator's `client_factory`,
now one layer up. Thirty-two tests in 1.7 s is what makes it cheap to keep them honest.
*`tests/test_api.py`*

### React state is replaced, never mutated — the canvas discipline again
`setMessages([...messages, m])` works; `messages.push(m)` renders nothing. React decides what
changed by comparing identity, so a new array *is* the signal. Same habit as the frozen
dataclasses on the Python side, for the same underlying reason. **The streaming corollary:**
inside a `for await` loop, `setReply(reply + chunk)` reads a `reply` captured when the loop
started and overwrites itself on every chunk. The functional form,
`setReply((prev) => prev + chunk)`, asks React for the current value.
*`frontend/app/components/Conversation.tsx`*

### `useRef` for what must survive a render without causing one
The session id has to persist across renders and nothing on screen depends on it, so it is a
ref. In state it would repaint the whole conversation every turn for nothing. Refs are also
the escape hatch for real DOM work: measuring `scrollHeight` to grow the textarea has no state
equivalent. *`Composer.tsx` `resize`, `Conversation.tsx` `sessionId`*

### Reading SSE by hand, because `EventSource` is GET-only
The browser's native SSE client cannot send a request body. Moving the stakeholder's message
into a query string would have made it fit, and would have put their words in every URL, log
and referrer on the way — so it was never really an option. `fetch` plus
`response.body.getReader()` is about forty lines. **The part worth remembering:** SSE frame
boundaries and network chunk boundaries have nothing to do with each other, so one
`event:`/`data:` pair can arrive split down the middle. Everything after the last blank line
stays buffered until its terminator shows up. *`frontend/lib/api.ts` `streamTurn`*

### The wire format stops at the client module
`streamTurn` yields the same four event types the Python orchestrator yields: `text`,
`activity`, `done`, `error`. No component knows SSE exists, and an unrecognised event name is
skipped rather than treated as a failure, so the backend can add one without breaking a
deployed frontend. Same reason `_translate_stream_event` exists on the Python side — two
translation layers, each stopping a wire format from leaking into the thing above it.

### Derived state beats a flag someone has to clear
Whether to offer "a reviewer has a question" is not `review_status == "sent_back"`; that stays
true after the question has been asked. It is `send-backs recorded > turns with
origin="reviewer"`, computed from the snapshot on every request. Nothing to set, nothing to
forget to unset, and correct after a reload, a restart, or a second send-back. The canvas's
`covered`/`missing` and `review_status` itself are the same idea — this one was only easier to
get wrong because the tempting field was sitting right there. *`blueprint/api.py`
`_pending_note`*

### The snapshot is the audit trail, so a partial restore must not narrow it
A restored session has no live `TurnResult` for work another process did. That is fine for
running and was silently fatal for saving, because `to_dict()` then emitted only this
process's turns. Prior turns are now carried forward verbatim, as the dicts they were written
as. General rule: when an object can be rebuilt from a snapshot, check what its *serializer*
does with the parts that were not rebuilt. *`blueprint/orchestrator.py` `_restore`, `to_dict`*

### Two humans, two logs
`review.py` holds the reviewer's decisions, `feedback.py` the requester's ratings. Both
append-only, neither collapsing history — a rating that changed is information, so `current`
is a view over the log rather than a replacement for it. Keeping them separate means the
reviewer surface and the requester surface never have to filter each other's data out.

### A secret the browser must not hold needs a server, not a convention
The reviewer routes need a bearer token. Any token reachable from browser JavaScript is a
token anyone can read out of the bundle, so the browser never gets one: it calls same-origin
route handlers, and those run on the Next server and add the header on the way through. The
proxy also relays the API's own status codes, so a 409 on an incomplete canvas still reads as
a 409 rather than a generic failure. *`frontend/lib/server/backend.ts`*

### `import "server-only"` turns a rule into a build error
The rule "do not import this from a Client Component" is exactly the kind that holds until
someone is in a hurry. The `server-only` package makes that import fail the build instead.
Same instinct as `tests/test_isolation.py` source-scanning every `ClaudeAgentOptions(` for
`env=AGENT_ENV`: when a mistake would be invisible and expensive, make it impossible rather
than discouraged. **Verified, not assumed:** built the app and grepped `.next/static` for the
token. Absent. The only hit anywhere was the Turbopack disk cache, which is never served.

### Who did it is the server's answer, not the client's
The API takes `reviewer` in the decision body, which means whoever calls it decides whose
name lands in the audit log. The route handler now reads the name from an httpOnly cookie and
overwrites whatever the body claimed — the page says *what* was decided, the server says
*who*. Tested by posting a decision with a forged name alongside a valid cookie: the cookie
won. **What it is not:** authentication. Anyone who can reach the app can set the cookie. It
makes the recorded name non-forgeable *from the page*, which is worth having and is not the
same as protecting the queue. *`frontend/lib/server/reviewer.ts`*

### The expensive nicety is a button, not a default
A model-written spec title costs about a cent. Generating one per queue row would cost that
on every page load for rows nobody opens. `rewrite title` calls the summarizer on the one
spec a reviewer is actually reading. Where a feature costs money per use, the default is off
and the affordance is visible.

### A stored format has history, and something has to know it
The snapshot grew over nine build steps: `match` in step 2, `open_gaps` in step 4, `reviews`
in step 5, `build` in step 6, `feedback` in step 8, `origin` on turns somewhere between.
Twelve top-level keys and two turn-level keys are missing from at least one run on disk. The
snapshots are the audit record, so they are not migrated — the absence is real and stays
real. One function at the client boundary fills defaults, and nothing downstream has to know.
An old run showing no Classification section is correct: it genuinely has none.
*`frontend/lib/admin.ts` `normalize`*

### A TypeScript type on network data is a claim, not a check
`response.json()` is `any`, so `json<Trace>(...)` asserts a shape rather than verifying one.
I declared every key required, the compiler believed me, and the page crashed on the first
older snapshot it met. Reading it as `Partial<Trace>` and normalizing into `Trace` puts the
compiler back on the right side of the problem: it now refuses to let a missing key through
unhandled. The Python side has had this right since step 1 — every `from_dict` validates
rather than assumes.

### Read-only is a design decision, not a missing feature
The evals panel shows recorded runs and cannot start one. A suite costs real money and takes
minutes, and a page that can spend money by being clicked will eventually be clicked. The
command to run one is printed next to the table instead. Same reasoning as `force=true` on a
rebuild: where an action costs money, the default is off and the affordance is explicit.

### The admin view is where the earlier bugs became visible
`938bd51e` renders "10 turns" in its header and "Turns (2 stored)" above the list — the step 8
resume bug, in a run that predates the fix. The trace shows both numbers rather than
reconciling them. A debugging surface that quietly papers over inconsistency is worth less
than one that displays it.

### Two lists that must agree should be one list
`named_integration` derived its system names from the department skills. `writes_to_system`
matched against a hand-written literal list of nouns. They started aligned and drifted until
74 named systems and 8 generic types were visible to one rule and invisible to the other — so
a spec that wrote into BambooHR was shown as `elevated` rather than `high`. The fix was not a
longer literal list; it was deriving both from the same source so they cannot diverge again.
A unit test now asserts the containment directly, because the next drift would be just as
silent. *`blueprint/review.py` `_write_targets`*

### One flag can hide another's failure
Three rules had recall 0.00 and nothing had noticed, because every case that should have
raised them *also* raised `named_integration`, which reached the same risk level. The
composite answer was right while two of its inputs were dead. Suites therefore need cases that
isolate each rule — a flag on its own, with nothing else to cover for it — not just realistic
cases that happen to exercise several at once.

### The eval is allowed to correct the labels, and allowed to correct the code
Of the disagreements in the first risk-gate run, four were the code being wrong and three were
my labels being wrong: I had assumed `needs_extra_scrutiny` was reserved for named
integrations when it is simply `level >= elevated`. The skill-matching suite set the precedent
in step 2 by relabeling `hr_onboarding_packets` when both models disagreed with the label.
What makes either direction honest is that every case carries a `note` with its rationale, so
a disagreement is settled by reading rather than by whoever edits the file.

### "Smaller model, cheaper" is a hypothesis, not a fact
Haiku 4.5 on the completeness checker cost **the same as Opus** ($0.0070 vs $0.0069) and took
3.5x longer, with a p95 of 24.7 seconds inside a tool call. Structured output against a rubric
is not the workload where a small model saves money. The benchmark existed to check the
assumption and the assumption was wrong — which is the entire value of having run it.

### Pick the metric before running, and pick it by consequence
Every suite here names one headline number chosen from what the failure costs, not from what
is easy to compute. The risk gate's is the scrutiny false negative, because a false positive
costs a reviewer thirty seconds and a false negative is an unreviewed build. The checker's is
the missed gap, because an invented gap merely annoys. Both suites report the opposite
direction too — the point is knowing which one decides.

### An abstraction earns its keep on the day you swap it
`RunStore` was a Protocol with one implementation for five steps, which is exactly when an
abstraction looks like overhead. Swapping in SQLite changed one function — which store
`create_app` builds — and no route, no handler and no test assertion. What did change was
caught by the type checker, not by a failing test: `MemoryStore` in the test suite stopped
satisfying the protocol the moment it grew two methods, and mypy said so before anything ran.
*`blueprint/store.py`, `blueprint/api.py` `_default_store`*

### Current state and history are different tables
A snapshot is overwritten every turn; an audit event happened and never changes. Putting "who
approved this" in the snapshot means the next approval erases the last one. Keeping them apart
is why the trail can show `send_back` *then* `approve` while the spec shows only the current
status — and there is a test asserting a save does not disturb the events.

### Derive the machine's story, record the human's as it happens
Human decisions are written when they occur, with the authenticated account attached, because
"who" exists only at that moment. The agent's steps are derived from the snapshot on save,
because the snapshot already *is* the record of what the agent did — double-writing would
create two sources that can disagree. The payoff showed up in the migration: 12 runs that
predate the audit table got 96 events derived from their snapshots, so history does not begin
at whenever the feature landed. *`blueprint/store.py` `build_events`*

### A redactor should be honest about being a filter, not a guarantee
It catches what has a mechanical shape — emails, phone numbers, SSNs, card and account
numbers — and leaves prose alone. A regex chasing names would either miss most of them or
shred the conversation. The tests assert both directions, and the second one matters more
than it looks: "30 to 40 new hires a month" and "20-30 minutes each" must survive, because a
store that eats the answer to *how many* and *how often* has destroyed the canvas to protect
a phone number. The module says what it does not do rather than implying coverage.

### A precise type that nobody can use is not precision
Typed the redactor's walker as a fully recursive `Json` union. Correct, and it produced 66
errors at call sites, because every caller then had to narrow a union before indexing. The
fix was to keep the recursive alias *inside* the walker and type the public function as what
callers actually hold — a snapshot dict. Precision belongs where it constrains the tricky
code, not where it taxes every reader.

### Auth: a gate, not a check per route
`proxy.ts` decides once for `/review`, `/admin` and their API trees. A new admin route is
protected by *existing*, not by someone remembering — the same instinct as `test_isolation.py`
scanning every `ClaudeAgentOptions(`. The decision handler checks again anyway, because a
handler that assumes a gate in front of it breaks quietly the day the matcher is edited.
**Fail closed:** with no session secret or no accounts, every protected route is refused.
The tempting default is to stay open until configured, which is how a staging box ends up
public. *`frontend/proxy.ts`, `frontend/lib/server/auth.ts`*

### Write the README when the architecture stops moving, not before
It was tempting at step 8, when there was something to show. Steps 9 through 12 then added a
server side to the frontend, real auth, a database and an audit trail — every one of which
would have invalidated a paragraph. The cost of waiting was nothing; the cost of not waiting
is a document that quietly lies about its own system.

### An honest-limits section is worth more than the omission
Four departments, one Builder template, two of four eval measures missing, redaction as a
filter rather than a guarantee, no rate limiting, no tests on the requester view. A reader
finds all of that in ten minutes anyway. Saying it first converts a gap someone discovers into
a judgement they can see was made — and it is the same instinct as the docstrings that say
what a module does *not* do.

### Verify the diagram renders; do not assume the renderer copes
The first Mermaid diagram was syntactically valid and visually useless — sprawling, edges
crossing, unreadable without zooming. Rendering it through mermaid 11 in a browser took two
minutes and showed both that it parsed *and* that it needed rebuilding as a linear flow. A
README diagram that needs zooming is not doing its job, and "it's valid syntax" would not have
caught that.

### A signed token proves who issued it, not that the account still exists
Deleting the dev accounts is what surfaced it: a deleted admin's cookie still opened
`/admin`. Stateless sessions are self-contained by design, which is exactly why something has
to re-check them — the signature was never in question, the account was. Two layers were
wrong at once: a user store cached on first read and never re-read, and nothing consulting
that store after issue. The fix goes in `readSession` rather than at each call site, for the
same reason the auth gate lives in `proxy.ts`: a check every caller has to remember is a check
someone will forget. *`frontend/lib/server/auth.ts`*

### A test that has never failed has not been shown to test anything
Wrote 27 tests for the auth core, then reverted the account-validity check and re-ran them.
Exactly the four tests describing that behaviour failed, and restoring it passed all 27. That
step took a minute and is the only evidence the tests are wired to the thing they claim to
cover — a green suite proves the code passes, not that the test would notice if it stopped.
*`frontend/lib/server/auth.test.ts`*

### Import the module the way production imports it
`auth.ts` opens with `import "server-only"`, which throws in plain Node. The temptation is to
mock it, and then the tests exercise a copy rather than the real module. `server-only` exports
an empty module under the `react-server` condition, which is exactly how Next resolves it on
the server — so `node --conditions=react-server --test` runs the genuine file with no mocking
at all. Worth checking what a blocker actually *is* before working around it.

### Security controls are tested, not assumed
`gitleaks` in pre-commit and CI. Tested by staging three key shapes and a normal string; the
first attempt exposed that the default Anthropic rule matched only one exact key length, so
project rules were added. "It's configured" is not evidence.

---

## Mistakes and what they taught

| What happened | What it taught |
|---|---|
| Said `allowed_tools=[]` removes tool schemas from context. It only controls permission; `tools=[]` removes them. | Read the option docstrings in the installed version; two similarly named options rarely do the same thing. |
| Accumulated `total_cost_usd` per turn; it is a running session total. Costs looked plausible but were double-counted. | When a number "looks fine," check its definition anyway. The monotonically increasing column was the tell. |
| `max_turns=1` on the classifier passed a 3-case probe and failed in the 120-call benchmark. | Structured output costs a hidden turn. Small probes hide intermittent failures; repeats catch them. |
| Orchestrator assumed error results are yielded; the SDK raises and the subprocess exits. | The happy path was tested; the failure path was not. Read the SDK source for the failure contract. |
| First gitleaks test "blocked" the commit, but ruff did the blocking; gitleaks passed a plausible key. | Test a guard with the thing it must catch, in the form the guard actually reads (staged, not `--files`). |
| `StrEnum.title` property shadowed `str.title()`. No linter flagged it; `mypy --strict` did. | Type checking finds a class of bug linting cannot. |
| Patched files via shell heredocs; `\n` escapes turned into real newlines, twice. | Use the editor tool for anything containing escapes. Cheap lesson, repeated once too often. |
| Test set a fake-client flag before `start()` created the client → `IndexError`, initially misread as a logging problem. | Read the traceback line before theorizing. |
| Windows console encoding crashed the CLI on `✓`, and the crash happened before the snapshot was written. | Force UTF-8 on stdout; write the most valuable output in a `finally`. |
| Ran the real classifier from a unit test by accident (default `match_fn`). | Every external dependency needs an injection point with a fake default in the test helper. |
| Typed the hook's return as `dict[str, Any]`; mypy rejected it against the SDK's `HookJSONOutput` union. | When an SDK publishes TypedDicts for a contract, use them: the type error is the documentation. |
| Tests passed but took 22 s instead of 2 s: the fake client's real record tool called the real checker, which spawned real subprocesses that failed silently. | Test *duration* is a signal. Every new external call needs a fake default in the test helper before the first test run, not after. |
| The Builder's report quoted my own memory notes; every agent since step 2 had been carrying them. | `setting_sources=[]` is not the whole isolation story. Measure what the subprocess actually loads (`get_context_usage`) instead of assuming; then enforce the fix structurally with a source-scanning test. |
| Gave the verification subprocess an empty environment to keep secrets out; Python could not start on Windows. | Strip the secrets, inherit the rest. "Secure" and "works" are both requirements. |
| POSIX-mode `shlex` stripped the backslashes from a Windows path, so an absolute path outside the workspace passed the guard. | Normalize separators before parsing; test the guard with real Windows paths, not just POSIX ones. |
| Per-turn cost showed `$-0.1161` on the first resumed turn. The SDK's running total restarts in the new subprocess; the delta against the restored total went negative. | Read every number in a trace, not just the ones you are looking for. Cost across a process boundary needs an explicit offset. |
| Shell heredoc patches mangled `
` escapes for the fourth time (CLI rewrite, orchestrator framing string). | Rule, finally applied: files containing escapes are written with the editor tool, never through a shell heredoc. |
| Started writing the SOP server against `FastMCP`, which no longer exists in `mcp` 2.x. | Probe the installed library's API before writing against a remembered one; one `inspect` call saved a rewrite. |
| Wrote a ranking test with a query the corpus phrased differently ("three" vs "3") and full of generic terms; it failed for two reasons at once. | Read the per-term scores before deciding whether the ranker or the test is wrong. Here it was both: the test was unrealistic *and* the ranker needed IDF. |
| The orchestrator set `self.session_id` from every `ResultMessage`. Harmless with the real SDK, which echoes the id back — but the API uses that id as its registry key *and* its snapshot filename, so one divergent result would have stranded a run under a name nothing looked up. | A value that two subsystems treat as authoritative must have one owner. The caller's id is now pinned and a mismatch is logged, never adopted. Found by asserting on the id in an API test, not by reasoning about it. |
| Cached the user store on the file's mtime alone. Filesystem mtime resolution is coarse enough that two writes inside one tick would serve the first one's contents indefinitely — which would have made the account-validity tests flaky and, more quietly, made a fast edit to `users.json` invisible. | Noticed while writing tests that needed to rewrite the same file twice. Tests exercise timings that production rarely does, which is a reason to write them beyond the obvious one. |
| Wrote a verification command that could not fail: `git log -S '<employer-name>' \| head && echo FOUND` printed FOUND on a clean result, because `head` exits 0 on empty input. | Caught only because the answer contradicted a second check. A verification whose failure mode is a false pass is worse than none — assert on a count, not on an exit code. |
| Built a session system where deleting a user changed nothing until the process restarted, and a deleted user's cookie kept full admin access for its remaining 12 hours. | Found by deleting the dev accounts and idly checking whether the old cookie still worked. "Is the thing I just removed actually gone?" is worth asking after every deletion, not just satisfying ones. |
| Broke `cli.py` by putting an escape sequence through a shell heredoc — the fourth or fifth time, and there is already a rule in this log saying not to. | The rule was right and I did not follow it. Escapes go through the editor tool. Writing a rule down is not the same as having internalised it, and the tell is that this entry could have been copied from the earlier one. |
| Let `ruff --fix` strip imports I had just added but not yet referenced, then watched F821 fire on the code that used them a minute later. | Auto-fix acts on the file as it is, not on the file as intended. Add the import and its first use in the same edit, or run the formatter after the code is complete rather than between two halves of one change. |
| A regex meant to insert three fields after `cors_origins=` matched `cors_origins=tuple(`, which spans lines, and injected them into the middle of a generator expression. | Anchoring on the start of a multi-line expression is anchoring on nothing. For structured edits into real code, match the whole expression or use the editor tool. |
| Three risk rules had not fired for anyone, ever, and the unit tests passed the whole time. The tests exercised each rule with a sentence built to match its regex; nothing checked the regex against the vocabulary the rest of the system actually uses. | A test written from the implementation tests the implementation. The labeled cases were written from the *problem* — "a stakeholder says they write into BambooHR" — and that is what found it. Both are worth having; only one of them would have caught this. |
| `p95` indexed with `int(0.95 * n)`, so at n=2 it returned the smaller value and p95 came back below p50. Spotted in a two-call smoke run, not in the metrics code. | A number that is impossible on its face — p95 under p50 — is the cheapest kind of bug to catch and the easiest to scroll past. Sanity-check the harness on a tiny input before trusting it on a big one. |
| Hit the same YAML trap twice in one sitting: an unquoted scalar containing `": "`, then one starting with a quote character. Fixed the first by hand and did not sweep for the second. | When a class of bug shows up once in authored data, sweep the whole file class immediately. The sweep took four lines and found the second instance I had already written. |
| Typed the admin trace as if every snapshot key were guaranteed, then watched the page crash on a step-6 run with no `feedback` key. Twelve keys drift across the runs on disk; the first one I opened happened to be the one that broke. | Data that has been written by more than one version of the program has more than one shape. Survey what is actually stored before writing the reader — one script over `runs/*.json` listed every drifting key in seconds, after the crash rather than before. |
| Bound `root: Path = EVALS_DIR` as a default argument, so the module attribute could never be overridden — including by the test that was meant to point it at a tmp_path. The test read the real evals directory instead and failed with a confusing KeyError. | A default argument is evaluated once, at import. Anything meant to be overridable is read at call time. The failure looked like a bad assertion and was really a binding-time bug. |
| `compile_spec`'s title fallback took `turns[0]["user_text"]` with no origin filter, so a spec whose first surviving turn was a send-back got titled with the reviewer's private note. Spotted by reading the first real queue I rendered. | The same leak I had already fixed once, in a second place. After fixing a boundary, grep for every other reader of the same field — `user_text` had two consumers and I patched one. |
| Chased two blank screenshots on the review page before measuring. `getBoundingClientRect()` said the buttons were at 844px inside a 914px viewport, i.e. fine; the pane's capture was simply unreliable at that scroll position. | Second time this cost me a detour. The rule is now: a blank screenshot is a claim to verify, not an observation. `read_page` and one measurement settle it faster than another guess. |
| Resuming a session wiped its transcript: `_restore` does not replay turn results, so the next `to_dict()` wrote one turn over ten. Present in the CLI's resume since step 5, found only by reloading a resumed conversation in a browser and seeing nine messages gone. | A partial restore is a decision about the *object*, and silently also a decision about everything that object serializes. The round-trip tests asserted the canvas and the counters survived. Nothing asserted the transcript did. |
| Offered "a reviewer has a question" forever, because `review_status` stays `sent_back` after the question is asked — and a reload would have put the same question to the requester twice. | The field that names a state is not always the field that answers "is there work left". Derive that from what actually happened, not from the nearest enum. |
| Then fixed it by swapping the new predicate in everywhere the old one appeared, which disabled the composer and locked the requester out of answering. | One rename, two meanings. When a boolean starts answering a second question, that is a second boolean. |
| Chased a blank screenshot through two "fixes" to `scrollIntoView` before measuring anything. The blankness was a capture artifact; the real bug was beside it, a sticky composer floating over the last element on the page and hiding the reopen button completely. | Measure first. One `getBoundingClientRect()` on the two elements answered what two guesses had not. The scroll changes were kept because they are right for other reasons, which is not the same as having been diagnosed. |
| Ran uvicorn with `--reload` to save restarts; every session create then failed with an empty `CLIConnectionError`. The reloader runs the app in a `multiprocessing` spawn child and the SDK could not start its CLI from there. | An empty error message is still evidence — it placed the failure in spawning rather than in our code, and the surviving process's command line named the cause outright. Convenience flags change the process tree, which matters when the app's whole job is spawning processes. |
| `POST /builds` guarded with "is a build running?", which read as idempotent and was not: between two requests the first build finished, so the second started a fresh one and overwrote a ~$0.46 workspace. | The test I wrote to confirm the guard is what disproved it — the assertion I expected to be trivial (`len(build_calls) == 1`) was the one that failed. State that changes on its own needs the *finished* case handled explicitly, not just the in-flight one. |

---

## Deferred and open

Kept as a list rather than quietly dropped. Everything here is a decision, not an oversight —
the README's "Honest limits" is the reader-facing version of this section.

Items completed during the build are removed rather than struck through; the commit history
and the progress table are where "when did this get done" lives. Six entries were pruned on
2026-09-28 after they had sat here for several steps already finished: reviewer identity (now
the authenticated account), transcript redaction and retention (step 12), requester feedback
(step 8b), the checker-model benchmark and the risk-gate eval (step 11), and how small a
prototype should be (decided per output format in step 6).

### Correctness and coverage

- **Two of CLAUDE.md's four eval measures are not built.** `risk_gate` covers the
  false-negative check and `completeness` covers per-category capture quality. Still missing:
  (a) **discovery completeness end to end** — were all seven categories actually captured over
  a whole conversation — and (b) **example relevance** plus the **rubric-scored judge
  comparison of a compiled spec against a gold one** (completeness / accuracy / actionability,
  never an overall score). Both need a *stakeholder simulator*: a second model playing the
  requester from a persona brief, because fixed reply scripts break as soon as the agent adapts
  its questions. Roughly $0.30 per case including the judge, so a 20-case run is $6–8 — a real
  decision rather than a default, which is why it is here and not done.
- **The requester view has no automated tests.** The backend has 434; the React has a type
  checker and a linter. Every UI bug in this project was found by clicking. Playwright against
  the real API would have caught the sticky-composer overlap and the locked-out composer.
- **Requester 👍/👎 as judge-validation labels** needs enough real feedback to be worth
  anything. Two ratings exist so far.

### Not exercised

- **The Bedrock auth path.** `.env.example` documents it; every run in this project went over
  the Anthropic API. Worth proving before any README claims both work.
- **Redaction against text that actually contains an identifier**, outside a unit test. The
  synthetic conversations have none, so migrating all 12 runs redacted nothing.
- **The build button, from the UI.** The endpoints behind it are tested and both guards were
  checked live through the proxy, but a real build costs ~$0.46 and that was not spent.

### Deployment shape

- **Auth has no rate limiting.** scrypt makes each attempt expensive, but nothing caps how many
  attempts `/api/auth` will take.
- **Signing out a user you want to keep** still means rotating `BLUEPRINT_SESSION_SECRET`.
  Deleting or demoting an account takes effect on the next request — `readSession` checks the
  account still exists with the same role — but there is no revocation list for "log this
  person out without touching their account".
- **The requester UI still has no automated tests**, though `lib/server/auth.ts` now has 27.
  Playwright against the real API is the remaining gap.
- **The session registry is in-process**, so the API runs as exactly one worker. A second
  uvicorn worker would route a requester's follow-up to a process that has never heard of their
  session. The SQLite store is where a shared session lookup would live; until then this is a
  documented single-process deployment, not an accident.
- **The web-search allowlist is federal `.gov` only.** State sites are a per-deployment setting
  still to be added.

### Content and polish

- **HR skill file and rubric review by the domain owner.** `skills/hr/SKILL.md` and
  `CATEGORY_RUBRIC` in `canvas.py` decide what the agent chases, and I wrote both.
- **The spec title fallback is the first 80 characters of the opener**, which reads badly in
  the queue. The reviewer view has a `rewrite title` button that calls the summarizer for ~1¢
  on demand; making it automatic per row would cost that on every page load for rows nobody
  opens, so the queue still shows the ugly version.

### Kept as evidence

- **One run's transcript was lost** (`32ddecd8`) to the resume bug in step 8, before it was
  found. Left as it is rather than reconstructed — the admin view showing "10 turns" beside
  "Turns (2 stored)" is the clearest possible record of what that bug did.
