# Architecture (living document)

This repo implements **V0.1–V0.3** of the Personal AI OS, plus one V0.4 slice (tasks + audit log). The rest of V0.4+ stays gated until the current slice is used and approved.

## What exists now

```
User (CLI)
  → AgentLoop (JSON decisions, max steps, token budget, traces)
      → LLMClient (OpenAI-compatible; secrets stay in the client)
      → SafetyLayer (schema, allowlist, permission, risk vs autonomy)
      → ToolRegistry (calculator, time_now, echo_note, task_add, task_list, task_complete)
      → AuditLog (data/audit.jsonl, append-only, write-ahead for tool decisions)
Short-term memory: Conversation list in RAM only
Long-term memory: TaskStore (data/agent.db, SQLite) — tasks only so far
```

### Level 4 grants

`AUTONOMY_LEVEL=4` on its own auto-runs nothing beyond `SAFE` tools. The first
`CONFIRMATION` call in a request asks as usual, with a grant offer attached. If you
approve, `SafetyLayer` grants that **one tool name** for the **current request**, for
5 uses including the approved one (`GRANT_MAX_USES`). After that, or in the next
request, it asks again. Only offers the safety layer issued can become grants. The loop
passes your answer back and decides nothing itself. Grants live in memory only, are
audited (`grant_created`, `grant_id` on `tool_review`), and never apply to `HIGH_RISK`.

### Repeated calls

- **Reads** (`SAFE` tools): the loop does not re-run a call identical to the previous
  successful one (nothing changed in between). The model gets a `repeated` observation;
  a second repeat in a row stops the run (`repeated_call`).
- **Writes** (`CONFIRMATION`/`HIGH_RISK`): a call identical to one already *started* in
  this request (validated args, so `" x "` equals `"x"`) always asks again, at every
  level and even under a Level 4 grant. "Started" covers timeouts and errors, where
  the first call may still have acted. Asking, not blocking, keeps deliberate
  duplicates possible.

### Secrets and child processes

The API key is a `SecretStr` in `Settings` (printing or dumping settings shows
`**********`) and is removed from `os.environ` once loaded. Provider errors are
redacted before they become an `LLMError`. Any tool that starts a program must use
`app.tools.process.run_command`: an **allowlisted** environment (so unknown secrets
never reach the child either), arguments as a list with no shell, and a timeout that
kills the child. `tests/test_process.py` fails if anything else in `app/` starts a
process.

### Audit trail

Every run writes `run_start` → (`tool_review` → `tool_confirmation`?) → `step`… → `run_end`.
A tool that timed out but completes later adds `tool_late_finish` (`ok` or the exception
type), because the `timeout` step alone would suggest the action never happened.
`tool_review` is written before the tool runs; if the write fails, the tool is refused
(fail closed). User text, raw args, tool output, tool exception text and model output
are never written: only an args hash, the safety reason, the exception *type* for tool
errors, and a fixed `reason` code (`max_steps`, `token_budget`, `invalid_output`,
`repeated_failure`) when a run stops early. One field is model-written: the step
`summary` (truncated), kept on purpose because it explains *why* a tool was chosen. It
can paraphrase what you asked, so skim it before sharing the file.
If the agent crashes mid-write, the next write puts the cut-off line on its own line
so no later event is lost, and `AuditLog.read()` skips damaged lines with a warning
that names only their line numbers (`read(strict=True)` raises instead).

The LLM **proposes**. Python **decides**. Tools **act** only after safety review.

## Why this split

If the model invents a destructive command, it still cannot run unless a registered tool exists, arguments validate, permission is granted, and autonomy/confirmation allow it. Permissions fail closed: `SafetyLayer` requires an explicit grant set (`DEFAULT_GRANTS` for a normal session), so a newly registered tool is denied until its permission is granted on purpose. Wrapping the vendor SDK lets tests use `FakeLLMClient` so CI never spends money or hits the network.

## Decisions

### D3: hand-written loop, JSON-in-text protocol (decided 2026-10-14)

**Hand-written, not a framework** (OpenAI Agents SDK, LangGraph, LangChain). The
safety guarantees depend on a fixed order inside `AgentLoop._act`: review →
write-ahead audit → confirmation → grant → duplicate check → timeout → output limit
and wrapping. With a framework, its tool execution would decide that order and
could change it with any update. Keeping the loop in our code keeps "the model
proposes, Python decides" readable and tested.

**JSON-in-text, not native tool calling.** The model replies with one JSON object
(`action`, `tool`, `args`, `summary`) as plain text. It works with every
OpenAI-compatible provider, including small local Ollama models, and its weak points
are covered: malformed output gets bounded repair retries, and tool output is
wrapped as untrusted `<tool_result>` data. Native tool calling (`role="tool"`
messages) would separate tool output more cleanly but means rebuilding the loop,
both API styles and about 70 tests, plus finding a home for `summary`. Supporting
both protocols was rejected: every safety rule would have to be right twice.

**Revisit** with the first tool that reads outside content (web pages, files,
email), where the `role="tool"` separation is worth the migration.

## V0.4+ (not built)

Do not add these until the previous milestone is approved:

| Version | What | Why wait |
|---|---|---|
| V0.4 | SQLite sessions, facts (~~tasks~~ done) | Need a working loop first |
| V0.5 | GitHub, files, Linux sandbox | Needs safety + confirmations |
| V0.6 | Knowledge graph, university, tutor | Needs durable facts |
| V0.7 | Planner, opportunity scout, research | Needs tasks + citations rule |
| V0.8 | FastAPI, auth, dashboard | Needs data to show |
| V0.9 | Docker, deploy, monitoring | Needs an API worth shipping |
| V1.0 | Personal AI OS | Composition of the above |

**Not now:** PostgreSQL, Redis, Neo4j, LangChain-as-core, voice, email sending, unrestricted shell, dashboard.

## Memory plan (when V0.4 starts)

- Facts and deadlines: tables (SQL), not vectors.
- Lecture PDFs: chunk + embeddings when ingest exists.
- Relationships: foreign keys first; a graph engine only if joins fail.

## Security invariants

- `.env` at repo root is gitignored. Never `app/.env`.
- API keys never appear in prompts or logs.
- HIGH_RISK always confirms.
- Agent code cannot rewrite `app/safety/`.
