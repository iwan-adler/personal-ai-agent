# Personal AI Agent

A secure, modular **Personal AI Operating System** that starts as a CLI assistant and grows into university, coding, and infrastructure help — one milestone at a time.

Current milestone: **V0.3** (conversation + tools + agent loop), plus the first **V0.4** slice: a persistent task list and an audit log. Facts, sessions, GitHub, Linux, tutor, dashboard, and deploy are **not built yet**.

## Tasks (persistent)

Ask in plain language, for example "remind me to submit the DBMS assignment by 2026-10-20", "what's due?" or "mark task 1 done". The model proposes a tool call. Python then validates it, checks permissions and asks you before anything is written.

| Tool | Risk | Permission | Notes |
|---|---|---|---|
| `task_list` | SAFE | `tasks.read` | Earliest deadline first |
| `task_add` | CONFIRMATION | `tasks.write` | `due_date` must be `YYYY-MM-DD` and not in the past; unknown fields rejected |
| `task_complete` | CONFIRMATION | `tasks.write` | Errors on unknown or already-done ids |

There is no delete tool on purpose.

Data lives in `data/` (gitignored):

- `data/agent.db`: tasks (SQLite)
- `data/audit.jsonl`: one JSON line per agent event (`run_start`, `tool_review`, `tool_confirmation`, `step`, `run_end`). It records the safety decision and reason, the risk, an args **hash**, the result status and the latency. It never records your messages, raw arguments or successful tool output. The safety decision is written *before* a tool runs; if it can't be written, the tool doesn't run.

Read the audit log:

```powershell
Get-Content data\audit.jsonl -Tail 20
```

## Setup

1. Create / activate the virtual environment (already present as `.venv` if you used it before).
2. Copy `.env.example` to `.env` and set `OPENAI_API_KEY` and `OPENAI_MODEL`.
3. Optional: set `OPENAI_BASE_URL` for Groq, Ollama, or another OpenAI-compatible server.
4. Install deps:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```powershell
.\.venv\Scripts\python.exe -m app.main
```

Type a message, then `quit` to exit. Token usage and a one-line action summary print after each turn.

**Expected:** a prompt `You:`. After you send a message, `Agent:` prints a reply plus `tokens=` and step summaries. If the key is missing you get `Config error: OPENAI_API_KEY is not set`.

## Test (no network)

```powershell
.\.venv\Scripts\python.exe -m pytest
```

**Expected:** all tests pass (`passed` in the pytest summary). Failures usually mean the venv is missing `pytest` — install requirements again.

## Autonomy

Set `AUTONOMY_LEVEL` in `.env`:

| Level | Behavior |
|---|---|
| 0 | Answer only (tools denied) |
| 1 | Suggest tools, do not run them |
| 2 | Ask `[y/N]` before tools (default) |
| 3 | Auto-run SAFE tools |
| 4 | Auto-run SAFE and CONFIRMATION; HIGH_RISK still confirms |

## Architecture

See [docs/architecture.md](docs/architecture.md).
