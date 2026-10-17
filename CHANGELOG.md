# Changelog

Koda is built in milestones, each gated on the previous one. Design decisions D1–D7
and their reasons are in [docs/architecture.md](docs/architecture.md).

## 1.1.0 (2026-10-17)

Koda becomes an interactive study partner in its own window.

**Study partner**
- Study Mode (`/study`): pick a subject, topic, difficulty, goal and mode (learn, revise,
  practice, quiz); a tutor without tools teaches in short steps, asks one question at a
  time and gives feedback. Python keeps the score (a verdict counts only for an answer to
  an open question) and the study time; sessions are saved (migration 9).
- Flashcard practice in the window (`/cards`): the same SM-2 rules as `/quiz`, every
  grade saved at once, "again" shown once more at the end of the round, keyboard
  shortcuts, next interval on each button, practice per course. No model involved.

**Chat**
- Answers rendered as Markdown (headings, lists, tables, code with a copy button) by a
  small built-in renderer that escapes everything first.
- Recent chats in the sidebar to reopen or delete; suggested prompts; follow-up buttons
  (Explain more, Give an example, Test my understanding).
- Your message appears at once with a "Thinking…" timer, Enter sends, errors are shown
  (before, a slow model or a refused request looked like nothing happened).

**Dashboard**
- Welcome with today's summary and your name; tasks you can add, edit, complete,
  reopen and delete; deadlines grouped (overdue, today, next 14 days, later); study time
  today and this week; progress per subject; the weekly planner.
- Study hours per weekday set right there. They override `STUDY_HOURS` (Reset returns
  to it) and apply at once to the dashboard, `/plan` and the model's `plan_week` tool.

**Desktop app and look**
- `scripts\install-shortcut.cmd` adds a Koda icon that opens the web UI in its own Edge
  window (own profile, no extensions, no console); closing the window stops Koda.
- New design: sidebar, chat bubbles, cards, violet accents, light and dark theme,
  works in narrow windows.

**Tools and models**
- `sandbox_run` (V0.5 slice 6): shell commands in a throwaway Docker container on a copy
  of the workspace, with no network, a read-only system, an unprivileged user and
  resource limits. `HIGH_RISK`; needs Docker Desktop and a digest-pinned `SANDBOX_IMAGE`.
- Local models (`OPENAI_BASE_URL` on 127.0.0.1 or localhost) get a 180 s default timeout
  instead of 60 s: on a CPU the first answer can take over a minute.

## 1.0.1 (2026-10-17)

- Flashcards: **hard** now follows Anki. Pure SM-2 still multiplied the interval by the
  ease after a hard answer (e.g. 38 days -> 99); now it grows by x 1.2, at least one day
  (a new card: 1 day), and the ease drops by 0.15. Again, good and easy are unchanged.

## 1.0.0 (2026-10-17)

The roadmap's pieces working together, checked by an end-to-end "study week" test
(course → imported notes → flashcards with sources → exam → plan → quiz → health
check → backup) in which only the model is scripted.

## V0.9: local operations (D7: stay local, no deployment)

- `python -m app.doctor`: read-only health check (settings, database integrity and
  schema, audit log, disk space, `.env` protection, dependency drift).
- Automatic daily backups of `data/` with SQLite's backup API, `python -m app.backup`
  to list, verify and restore (asks first, keeps your previous state, zip-slip safe).
- Start scripts in `scripts/` for Windows, macOS and Linux.

## V0.8: web interface (D5)

- Runs can pause for approval and resume later (one-time value, single use, expiry).
- `python -m app.web_ui`: FastAPI + HTMX on 127.0.0.1 only, token sign-in, Host /
  Origin / CSRF checks, escaped rendering, strict Content-Security-Policy.
- Read-only dashboard: plan, tasks, courses, flashcards, opportunities.

## V0.7: planner, web, scout, citations (D6)

- Deterministic weekly planner with time estimates and `STUDY_HOURS`.
- `web_fetch` for allowlisted domains only, with SSRF, redirect and DNS-rebinding
  protection; safe RSS/Atom parsing.
- Opportunity scout over your feeds, matched against your interests.
- Citation check: sources not read in the request are flagged.

## V0.6: knowledge, university, tutor (D4)

- Document library from `library/` with page-level citations; PDFs parsed in an
  isolated child process.
- Courses linking tasks, facts and documents (foreign keys as the "knowledge graph").
- Flashcards with SM-2 and a model-free `/quiz`.

## V0.5: files and GitHub

- "Taint": after reading outside content, Level 4 grants are suspended.
- Workspace file tools with Windows path safety; create (CONFIRMATION) and overwrite
  (HIGH_RISK, diff preview, checksum, backups).
- Read-only GitHub tools for allowlisted repos; publishing comments and issues
  (HIGH_RISK, separate token, secrets refused, full preview).
- The Linux sandbox was postponed until Docker was available (built after 1.0.1).

## V0.4: memory

- Tasks, chat sessions and long-term facts in SQLite with numbered migrations;
  bounded history window; append-only audit log.

## Audit fixes #1–#13 (on V0.3)

Repair retries, untrusted tool-output labels, fail-closed permissions, one source of
the autonomy level, timeouts, audit log hardening, Level 4 grants, repeated-call
detection, secret handling and a scrubbed subprocess runner, byte-safe truncation.

## V0.1–V0.3

Conversation, tools, the agent loop and the safety layer.
