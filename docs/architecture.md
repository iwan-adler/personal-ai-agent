# Architecture (living document)

This repo implements **V0.1–V0.4** of the Personal AI OS (V0.4: tasks, sessions, facts and the audit log). V0.5+ stays gated until the current milestone is used and approved.

## What exists now

```
User (CLI)
  → AgentLoop (JSON decisions, max steps, token budget, traces)
      → LLMClient (OpenAI-compatible; secrets stay in the client)
      → SafetyLayer (schema, allowlist, permission, risk vs autonomy)
      → ToolRegistry (calculator, time_now, echo_note, task_*, fact_save/search/list)
      → AuditLog (data/audit.jsonl, append-only, write-ahead for tool decisions)
Short-term memory: Conversation in RAM; only the last MAX_HISTORY_MESSAGES go to the model
Long-term memory: data/agent.db (SQLite, numbered migrations in app/memory/db.py)
  tasks · sessions + messages (what you typed + final replies) · facts (+ FTS5 index)
```

### Sessions and facts

- **History window.** The model gets the system prompt plus about the last
  `MAX_HISTORY_MESSAGES` messages. The window always starts at something the user
  said, so a `<tool_result>` or repair request is never sent without its call, and
  the current request is never cut. Old tool output therefore leaves the context.
- **Sessions** store only what you typed and the agent's final replies, never tool
  calls or tool results: an instruction planted in an old tool result cannot return
  in a later session. Each launch starts a new session; `/resume` continues one.
- **Facts** are saved only by `fact_save` (`CONFIRMATION` at every level, exact text
  shown) and reach the model only as `<tool_result>` data from `fact_search` /
  `fact_list`, never in the system prompt. Search queries become quoted plain words,
  so model text cannot act as FTS5 syntax. No tool deletes: only `/forget-fact` and
  `/forget-session`, typed by you, after a y/N prompt.
- Stored messages and facts are **unencrypted**; the audit log gets ids only.

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

### D4: keyword search first, local embeddings later (decided 2026-10-14)

Nothing is built yet; this frames document search for V0.6.

**Search: SQLite FTS5 keyword search first.** It is built into Python's `sqlite3`,
works offline, needs no new dependency and is strong on the exact terms lecture
material is full of (course codes, formulas, names). Embeddings are added only once
real searches show keyword search missing paraphrased questions; results are then
combined (hybrid).

**Embeddings: local via Ollama** (e.g. `nomic-embed-text`). Embedding through an API
would upload *everything* loaded, personal notes included, not just the excerpts a
chat question needs. `sentence-transformers` was rejected for pulling in PyTorch.

**Storage: SQLite (D1).** Vectors as BLOBs next to their text, compared with numpy.
That is milliseconds for a few thousand sections; the `sqlite-vec` extension can
replace the comparison past ~100k without a schema change. No Chroma, LanceDB or
pgvector: a second store for a size we will not reach.

**Rules from day one,** whatever is built:
1. Search results are untrusted data and reach the model as `<tool_result>`, like any
   tool output. PDFs can carry hidden instructions.
2. Loading documents is a `CONFIRMATION` tool limited to one configured folder;
   searching is a `SAFE` tool. Both go through the safety layer.
3. Loading documents is the first tool that reads outside content, so the D3 revisit
   (native tool calling) happens before it is built.
4. Search queries are user text: the audit log gets counts and hashes, not queries.
5. Every vector stores its embedding model and dimension; vectors from different
   models are never mixed.
6. Answers from notes cite their source (file and page).

### D5: CLI until V0.8, then FastAPI + HTMX (decided 2026-10-14)

**When: V0.8.** The CLI stays the only interface until then, as the roadmap says: a
dashboard needs data to show, and any UI is a large new attack surface.

**Stack: FastAPI with server-rendered HTML and HTMX.** It fits the roadmap, needs
little JavaScript and no frontend build, and keeps the logic on the server where it
is easy to secure. React was rejected unless the dashboard grows complex; Streamlit
and Gradio because their rerun-the-script model fits approve-and-continue poorly and
Gradio can publish public share links with one setting.

**First step: pending approval with resume.** Today `confirm(prompt) -> bool` blocks
inside the run, and `blocked_confirm` ends it, so the model must propose the call
again. A web page cannot hold a request open until you click. The loop and safety
layer will instead pause with an approval id, and your answer continues the *same*
run. The CLI keeps working on top of that.

**Rules for any web UI:**
1. Listen on `127.0.0.1` only, never `0.0.0.0`; no public share links.
2. Every request needs a session token, and the server checks `Host` and `Origin`
   (cross-site requests and DNS rebinding: any website open in your browser can
   send requests to `localhost`).
3. An approval counts only with its approval id plus a one-time value from a real
   UI action, so a replayed or made-up request approves nothing.
4. Model and tool text is always rendered escaped, never as raw HTML, so tool output
   cannot inject a fake "allow" button.
5. The audit log, safety layer and grants do not change: the UI is just another way
   to answer yes or no, like `input()` today.

## V0.4+ (not built)

Do not add these until the previous milestone is approved:

| Version | What | Why wait |
|---|---|---|
| V0.4 | ~~SQLite tasks, sessions, facts~~ done | Need a working loop first |
| V0.5 | GitHub, files, Linux sandbox | Needs safety + confirmations |
| V0.6 | Knowledge graph, university, tutor | Needs durable facts |
| V0.7 | Planner, opportunity scout, research | Needs tasks + citations rule |
| V0.8 | FastAPI + HTMX, auth, dashboard (see D5; starts with pending approval + resume) | Needs data to show |
| V0.9 | Docker, deploy, monitoring | Needs an API worth shipping |
| V1.0 | Personal AI OS | Composition of the above |

**Not now:** PostgreSQL, Redis, Neo4j, LangChain-as-core, voice, email sending, unrestricted shell, dashboard.

## Memory plan

- Facts and deadlines: tables (SQL), not vectors. Done in V0.4 (`tasks`, `facts`).
- Lecture PDFs: chunk + SQLite FTS5 keyword search when ingest exists; local embeddings later, only if needed (see D4).
- Relationships: foreign keys first; a graph engine only if joins fail.

## Security invariants

- `.env` at repo root is gitignored. Never `app/.env`.
- API keys never appear in prompts or logs.
- HIGH_RISK always confirms.
- Agent code cannot rewrite `app/safety/`.
