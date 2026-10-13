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

### Audit trail

Every run writes `run_start` → (`tool_review` → `tool_confirmation`?) → `step`… → `run_end`.
`tool_review` is written before the tool runs; if the write fails, the tool is refused
(fail closed). User text, raw args, tool output, tool exception text and model output
are never written: only an args hash, the safety reason, the exception *type* for tool
errors, and a fixed `reason` code (`max_steps`, `token_budget`, `invalid_output`,
`repeated_failure`) when a run stops early. One field is model-written: the step
`summary` (truncated), kept on purpose because it explains *why* a tool was chosen. It
can paraphrase what you asked, so skim it before sharing the file.

The LLM **proposes**. Python **decides**. Tools **act** only after safety review.

## Why this split

If the model invents a destructive command, it still cannot run unless a registered tool exists, arguments validate, permission is granted, and autonomy/confirmation allow it. Permissions fail closed: `SafetyLayer` requires an explicit grant set (`DEFAULT_GRANTS` for a normal session), so a newly registered tool is denied until its permission is granted on purpose. Wrapping the vendor SDK lets tests use `FakeLLMClient` so CI never spends money or hits the network.

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
