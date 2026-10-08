# Personal AI Agent

A secure, modular **Personal AI Operating System** that starts as a CLI assistant and grows into university, coding, and infrastructure help — one milestone at a time.

Current milestone: **V0.3** (conversation + tools + agent loop). Later memory, GitHub, Linux, tutor, dashboard, and deploy are **not built yet**.

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
