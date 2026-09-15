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
| 2026-09-12 | 1 | Canvas backbone, discovery agent with `record_canvas_answer` tool, `DiscoverySession` orchestrator with streaming turn loop, JSON-lines logging, CLI with replayable scripts. First live 10-turn HR conversation completes the canvas for ~$0.15. | `ac1fe68` |
| 2026-09-13 | 1 | `.gitattributes` pins LF line endings. | `8f2ae63` |
| 2026-09-13 | 2a | Four department `SKILL.md` files, strict loader. | `81d90ab` |
| 2026-09-13 | 2b | Structured-output department classifier, `department_examples` tool, orchestrator runs the classifier once `key_activities` is captured. | `d0c4636` |
| 2026-09-13 | 2 | Skills moved from `.claude/skills/` to `skills/`; path resolved from the package, not the CWD. | `4650b69` |
| 2026-09-14 | 2 | Classifier benchmark: 20 labeled cases, Opus vs Sonnet × 3 repeats. Opus kept on stability. One label corrected by the evidence. | `b98e85f` |
| 2026-09-14 | — | Hardening: SDK error containment, idle timeout, input validation, validated settings, pre-commit + CI with secret scanning. | `d9be267` |
| 2026-09-14 | — | Project gitleaks rules after the default rule missed a plausible key shape. | `5c28038` |
| 2026-09-14 | — | This learning log, and the CLAUDE.md rule to keep it current. | `ac08c32` |
| 2026-09-14 | — | Scoped web search (Tavily) as an in-process tool with a domain allowlist, plus a `PreToolUse` hook capping calls per session. First hook. Live run: one search, generic query, USCIS-only results, cited in the recorded summary. | `f4ffa8e` |

| 2026-09-14 | 3 | SOP library as an external stdio MCP server: eight synthetic SOPs, strict loader, IDF-weighted keyword search, `MCPServer` over stdio, wired via the SDK's `mcp_servers` stdio config next to the in-process canvas server. Live run: on "our onboarding checklist" the agent searched and read the packet-review SOP on turn 1, then asked whether the I-9 document inspection is part of the checklist pass. | `66c30bf` |

| 2026-09-14 | 4 | Completeness check and clarification loop: per-category rubric as data, a structured-output judge run inside the record tool so a gap becomes a follow-up in the same reply, a clarification budget of two per category, `is_ready_for_review` as the handoff decision, open gaps carried to the reviewer. Live run: "just the HR team" was flagged for dependents, the agent asked, the re-record passed. | `ab50855` |

| 2026-09-14 | 5 | HITL gate: compiled spec from the snapshot, rule-based risk with evidence per flag, append-only review log with validated decisions, send-back delivered through the SDK's `resume=` as a reviewer-framed instruction turn. CLI grew `review` and `resume`. Live round trip: send-back → resume → the agent asked for the intake system's product name → re-recorded as v2 → spec v2. | `a91b3af` |

State at last update: 203 tests, ruff + mypy `--strict` clean, steps 1–5 of 13 complete plus
hardening and web search. Next: step 6 (Builder subagent).

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

### Subagents (learned conceptually; not yet used)
**What:** `AgentDefinition(description, prompt, tools, model)` under `options.agents`. The
parent invokes it via the `Task` tool; it runs in a **fresh context to completion** and returns
one result. **Consequence:** a subagent cannot hold a multi-turn conversation with a human,
which is why discovery is the *main* session (Decision: Option A). Subagents are reserved for
run-to-completion jobs: the Builder (step 6).

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
| Per-turn cost showed `$-0.1161` on the first resumed turn. The SDK's running total restarts in the new subprocess; the delta against the restored total went negative. | Read every number in a trace, not just the ones you are looking for. Cost across a process boundary needs an explicit offset. |
| Shell heredoc patches mangled `
` escapes for the fourth time (CLI rewrite, orchestrator framing string). | Rule, finally applied: files containing escapes are written with the editor tool, never through a shell heredoc. |
| Started writing the SOP server against `FastMCP`, which no longer exists in `mcp` 2.x. | Probe the installed library's API before writing against a remembered one; one `inspect` call saved a rewrite. |
| Wrote a ranking test with a query the corpus phrased differently ("three" vs "3") and full of generic terms; it failed for two reasons at once. | Read the per-term scores before deciding whether the ranker or the test is wrong. Here it was both: the test was unrealistic *and* the ranker needed IDF. |

---

## Deferred and open

- **Bedrock auth path**: `.env.example` documents it; not yet exercised. Verify before step 7.
- **Transcript redaction**: stakeholder text lands verbatim in `runs/` and logs. Local-only
  today; step 12's SQLite store needs redaction and retention.
- **HR skill file and rubric review** by the domain owner: `skills/hr/SKILL.md` and
  `CATEGORY_RUBRIC` in `canvas.py` decide what the agent chases.
- **User feedback in the requester view** (step 8): per-message 👍/👎 with optional comment,
  end-of-conversation rating. Stored against `(session_id, turn)` in step 12; used as human
  labels for judge validation in step 11. Keyed off `TurnResult`, so no trace change needed.
- **Checker model benchmark**: same harness as `evals/skill_matching`, labeled (category,
  summary) → missing elements; decide whether a cheaper model holds.
- **Risk-gate false-negative eval** (step 11): labeled snapshots that *should* flag, run
  through `assess_risk`; the rules are deterministic so this is cheap and exact.
- **Spec title fallback is ugly** (first 80 chars of the opener); `--summarize` fixes it for
  ~1¢. The reviewer view should always use the summarized title.
- **Web search state sites**: the allowlist is federal only; state `.gov` sites are a per-
  deployment setting still to be added.
- **"How small is the prototype"**: decided per output format when the Builder is built.
