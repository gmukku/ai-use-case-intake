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

State at last update: 117 tests, ruff + mypy `--strict` clean, steps 1–2 of 13 complete.
Next: Tavily web search (route B, in-process tool + `PreToolUse` hook), then step 3 (SOP stdio
MCP server).

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

### Hooks (next up)
**What:** `PreToolUse` / `PostToolUse` / `Stop` / … Python callbacks that observe, block, or
modify tool calls; the `beforeEach` of the agent loop. **Planned:** per-session cap and query
log on web search; file-write gating for the Builder.

### Sessions (learned conceptually; not yet used)
**What:** sessions persist on disk; `resume=session_id` continues one later, `fork_session`
branches. **Planned:** reopening a conversation when the reviewer sends it back (step 5).

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

---

## Deferred and open

- **Bedrock auth path**: `.env.example` documents it; not yet exercised. Verify before step 7.
- **Transcript redaction**: stakeholder text lands verbatim in `runs/` and logs. Local-only
  today; step 12's SQLite store needs redaction and retention.
- **HR skill file review** by the domain owner.
- **Web search scope**: regulatory grounding only, off by default, always off in evals.
- **"How small is the prototype"**: decided per output format when the Builder is built.
