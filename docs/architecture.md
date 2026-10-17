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

### Files (V0.5)

`file_list` and `file_read` (SAFE, `files.read`) exist only when `WORKSPACE_DIR` is
set, and reach nothing outside it. Paths are checked twice
(`app/tools/workspace.py`):

1. **In the input model, without touching the disk**, so the safety layer denies a
   bad path during review: absolute paths and drives, UNC and device paths, `..`,
   any `:` (alternate data streams), Windows device names (`CON`, `NUL`, `COM1`…),
   names ending in a dot or space (Windows strips them, so `.env.` opens `.env`),
   and off-limits names (`.env*`, `*.pem`, `*.key`, `id_rsa*`, `.ssh`, `.git`).
2. **In the handler, on disk:** the resolved path (symlinks and junctions followed)
   must stay inside the workspace, off-limits names are checked again on it, and
   files with several hard links are refused, because a hard link can point outside
   without `resolve()` noticing.

Both tools are `reads_outside_content`, so the taint rule applies. Errors never repeat
the path, which keeps it out of the audit log. Known gap: another program could swap
a file for a link between the check and the open; the agent cannot create links.
`WORKSPACE_DIR` itself may not be a drive root, the home folder, or contain the
project (or the agent could read its own database and, later, change its own code).

**Writing** (`files.write`): risk is fixed per tool, so there are two tools.
`file_create` (CONFIRMATION) only makes a new file in an existing folder;
`file_overwrite` (HIGH_RISK: always asks, never grantable) only replaces an existing
one and requires `expected_sha256` from `file_read`, so the model can only overwrite
a file it has read, and nothing is written if the file changed after your "yes".
Both show a **preview** in the prompt (the content, or a unified diff) instead of the
raw args; the preview goes to the prompt only, never to the model or the audit log,
and a declined call returns a short fixed message so no diff is saved in a session.
Writes are atomic (temporary file in the same folder, then `os.link` for create,
which fails if the name appeared meanwhile, or `os.replace` for overwrite). Every
overwrite first copies the old version to `data/file-backups/` (outside the
workspace, last 20 kept); no backup, no overwrite. Types Windows runs on
double-click (`.exe .bat .cmd .ps1 .js .vbs .lnk .reg …`) are never written, and
there is no delete, append or mkdir tool.

### Courses (V0.6, the "knowledge graph")

As the memory plan says, the knowledge graph is foreign keys, not a graph engine:
migration 5 adds `courses` (short `code`, unique regardless of case, `name`,
`semester`) and an optional `course_id` on `tasks`, `facts` and `documents`
(`ON DELETE SET NULL`). Exams are tasks linked to their course, so a deadline lives
in one place. `course_list` (SAFE) shows counts and the next open deadline;
`course_add` / `course_update` (CONFIRMATION) change courses, with old -> new in the
prompt. Task, fact and document tools take an optional `course` code to link or
filter; an unknown code is an error, and without it they behave as before. Course
data is the user's own, so it does not taint. Only `/forget-course` deletes, and
it only clears links. A re-imported document keeps its course unless a new one is
given.

### Web access (V0.7, implements D6)

`web_fetch` (SAFE, `web.read`, `reads_outside_content`) exists only with
`WEB_ALLOWLIST`. The URL is checked during the safety review: https, port 443, no
user:password, a host name (no IP literal) that is an allowlisted domain or a
subdomain of one, compared normalised (lowercase, IDNA), so suffix tricks and
look-alike letters do not match. `app/web/fetcher.py` then resolves the name and
refuses it if **any** address is not public (loopback, private, link-local
including 169.254.169.254, shared, reserved, multicast, IPv4-mapped IPv6), and
connects to **exactly the checked address** while TLS still verifies the certificate
against the host name, which closes DNS rebinding (and bypasses system proxies).
Redirects are followed manually, at most 5, each hop checked again. No cookies,
no compression (`Accept-Encoding: identity`), 2 MB, text types only. HTML becomes
text with the standard `html.parser` (scripts and styles dropped) and only links
that pass the same URL check are returned. For feeds, `parse_xml_safely` refuses
any DOCTYPE or ENTITY declaration and non-ASCII-compatible encodings before the
standard parser (expat 2.8, which itself refuses entity expansion) sees them.

### Web interface (V0.8, implements D5)

`python -m app.web_ui` serves FastAPI with server-rendered HTMX on **127.0.0.1
only**, through the same `AgentLoop` (built by `main.build_agent`) in deferred mode,
so every safety step is the CLI's. `app/web_ui/security.py` enforces D5's rules
before any route: the Host header must be this server (DNS rebinding); every
request needs the session token, a 32-byte value printed as a link at startup and
then kept in an HttpOnly, SameSite=Strict cookie (the redirect drops it from the
URL, and uvicorn's access log is off); every POST needs a same-origin `Origin`, a
`Sec-Fetch-Site` of same-origin if sent, and the page's CSRF value; approvals carry
the approval id plus its one-time value. Jinja2 renders with autoescape, the CSP is
`default-src 'none'; script-src 'self'; style-src 'self'` (no inline code), and htmx
(vendored, 2.0.11) runs with `allowEval` and `allowScriptTags` off and
`selfRequestsOnly` on, so text from the model or tools can never become markup or
code. The page shows the saved session (messages and final replies), not internal
tool results. Tested over real HTTP against a local uvicorn, and clicked through in
a browser with a scripted model. `/dashboard` is read-only (no forms): the plan from
`compute_plan`, open tasks, courses, flashcard counts and new opportunities, whose
links (stored http(s) only) carry `rel="noopener noreferrer nofollow"`.

### Deferred approvals (V0.8, first step of D5)

`AgentLoop(defer_approvals=True)` does not call `confirm()`. A call that needs an
answer pauses the run (`RunStatus.AWAITING_APPROVAL`) with `run.pending`: an
approval id, a 32-byte one-time value (`secrets.token_urlsafe`), the same prompt the
CLI shows, and an expiry (15 minutes). `resume(approval_id, nonce, approved)`
continues **the same run**: the answer is audited, a grant offer is accepted on yes,
the tool runs, and the loop goes on. The value is compared in constant time; a
wrong one is refused and leaves the approval pending (guessing cannot cancel it); a
used or expired one is unknown. While paused, `end_run` is not called, so grants,
taint and duplicate history survive; an expired approval ends its run as `EXPIRED`.
The loop's counters live in a `_RunState`, and `_act` is split into review,
`_decide` (apply an answer) and `_execute`, so the CLI's blocking `confirm()` and the
deferred path share every safety step. Pending approvals live in memory only.

### Citations rule (V0.7)

Tools that read citable material declare it: `Tool.cites(args, output)` returns the
citations a call made available (`doc_search`: each hit's citation; `web_fetch`: the
requested and the final URL), collected per request in `AgentRun.sources`. After the
final reply, `app/citations.py` takes every bracketed part that looks like a
citation (a URL, `p. N`, `line N`, or a .pdf/.md/.txt name; `[optional]` and
Markdown links are ignored), normalises it (case, spacing, URL fragment and trailing
slash), and flags those matching no source with a line appended to the reply. The
audit log records only the count. The system prompt tells the model to cite exactly
as given and only what it read in this request. It proves a source was read, not
that it supports the claim.

### Opportunity scout (V0.7)

`FEEDS` (max 20) must each pass the `web_fetch` URL check against `WEB_ALLOWLIST`,
checked when settings load, so feeds cannot bypass D6. `opportunity_scan` (SAFE,
`reads_outside_content`) fetches them through the same pinned path with feed
content types, parses RSS 2.0 and Atom via `parse_xml_safely`, strips HTML from
summaries, drops non-http(s) links, and keeps entries containing an `INTERESTS`
keyword as a whole word (custom boundaries, so `c++` works and `python` does not
match `pythonic`). Matches go into a local cache (migration 8, `opportunities`, link
unique); a failing feed is counted, the rest still run. It is SAFE although it writes,
because it only caches read-only data and its only traffic is GETs to the user's own
feed URLs. `opportunity_save` (CONFIRMATION) creates `Apply: <title>` with an
optional deadline (never guessed from text) and course. Only `/dismiss` hides
entries, so injected text cannot make the model hide an opportunity.

### Planner (V0.7)

`plan_week` (SAFE, `tasks.read`) and `/plan` show a plan computed by a **pure
function** (`app/planner.py`: no database, clock or model), so the same input always
gives the same plan and the model only explains it. Each day's capacity is
`STUDY_HOURS` for its weekday (2 h if unset, said in the plan) minus 30 s per due
flashcard. Open tasks with a deadline and `estimate_hours` (remaining effort,
migration 7, set via `task_add` or `task_estimate`) are scheduled earliest deadline
first in whole quarter hours, until the day **before** the deadline. Warnings name
the exact shortfall, overdue tasks and tasks due today; tasks without an estimate or
deadline are listed, not scheduled.

### Tutor: flashcards (V0.6)

`card_propose` (CONFIRMATION, `cards.write`) saves up to 20 cards per batch, each with
question, answer and source citation; the prompt shows every card in full, and an
exact duplicate question in the same course is skipped. `card_stats` (SAFE,
`cards.read`) returns counts only: card text usually comes from documents (outside
content) and never goes back into the model's context. Studying is the CLI's
`/quiz`, which runs **without the model**, so it is free, fast and offline. Grades are
four buttons mapped to SM-2 quality (again 1, hard 3, good 4, easy 5): the interval
grows with the previous ease (1 day, 6 days, then interval x ease), "again" restarts
the card and shows it once more at the end of the session, and the ease never falls
below 1.3. In pure SM-2, "hard" still lengthens the interval; it only lowers the ease,
so later intervals grow more slowly. Every grade is saved at once (migration 6:
`cards`, plus a `reviews` log).

### Document library (V0.6, implements D4)

`doc_import` (CONFIRMATION, `docs.write`) reads `.md`, `.txt` and `.pdf` from
`WORKSPACE_DIR/library/` only, with the same two-layer path checks as the file tools
(every file re-checked, so a junction or hard link inside `library/` pulls nothing in
from outside). Unchanged files (same sha256) are skipped; a changed file replaces its
chunks in one transaction. It returns counts only, never names or content, so it does
not taint. `doc_search` / `doc_list` (SAFE, `docs.read`) are `reads_outside_content`;
every hit carries a ready-made citation (`file, p. N` or `file, line N`) that the tool
description tells the model to quote. Storage is migration 4: `documents`, `chunks`
and an FTS5 index; queries are reduced to quoted words like fact search.

PDFs are parsed by `app/tools/pdf_extract.py` (pypdf 6.19.0, hash-pinned) in a
**separate process** started through `run_command`: Python's isolated mode, the
allowlisted environment without keys, and a 60 s timeout that kills it, because
crafted PDFs can make parsers loop or exhaust memory. Limits: 50 MB and 1,000 pages
per PDF, 5 MB of text per file; encrypted PDFs are refused.

### GitHub (read-only, V0.5)

`github_issue_list`, `github_issue_read` and `github_pr_list` (SAFE, `github.read`,
`reads_outside_content`) exist only when `GITHUB_REPOS` is set. The allowed repos
are a fixed set of choices (`Literal`) in each input schema, so any other repo is
denied during the safety review, before a request is made. The client
(`app/tools/github_client.py`) uses the standard library with two rules: the host is
fixed to `https://api.github.com` and paths are built only from validated parts (no
SSRF, no path injection), and **redirects are refused**, with the token sent as an
unredirected header, because `urllib` would otherwise forward `Authorization` to
whatever host a redirect names. The optional `GITHUB_TOKEN` is a `SecretStr`, removed
from `os.environ` on load and redacted from every error. Issue text is capped and
counts as outside content, so the taint rule applies.

**Publishing** (`github_issue_comment`, `github_issue_create`; HIGH_RISK, `github.write`)
cannot be taken back, and the main risk is exfiltration: injected text getting the
model to post private facts or file content. So: only repos in `GITHUB_WRITE_REPOS`
(a subset of `GITHUB_REPOS`), through a separate client with a separate
`GITHUB_WRITE_TOKEN` that the read tools never use; text that looks like a secret,
or contains the value of a configured key or token, is denied during the safety
review; and the prompt shows the target with the issue's current title, the repo's
visibility, every @mention and the full text, uncapped. A "no" sends nothing. The
output is only the link and number, so it is not outside content.

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
Once a tool that reads outside content (`reads_outside_content=True`: files, GitHub)
has started in a request, grants are suspended for the rest of it: no use is spent,
none is offered, and every write asks. A read that failed or timed out still counts.

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

**Revisited 2026-10-14 (before V0.5's file tools): kept.** A `role="tool"` message
separates outside text a little more cleanly, but it does not stop a model from
following injected instructions; the safety layer is the real barrier, and the
migration cost is unchanged. The actual gap was Level 4 grants: injected text read
mid-request could spend the remaining uses without a prompt. The answer is the
**taint rule**: tools marked `reads_outside_content=True` suspend all grants for the
rest of that request, so every write after reading outside content asks.

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

### D6: web access only to allowlisted domains (decided 2026-10-17)

V0.7's opportunity scout and research need the web, the largest risk since
publishing. Besides injection at scale, a model that picks URLs can **leak data in the
URL itself** (`https://attacker.example/?notes=...`): merely fetching it sends the
data, with no text for the user to approve. It could also reach the local network
(`localhost`, `192.168.x.x`).

**Chosen: feeds plus allowlisted pages.** RSS/Atom feeds come from a fixed list the
user configures, and `web_fetch` reaches only domains on `WEB_ALLOWLIST`: HTTPS only,
redirects only within the list, every resolved IP checked against private, loopback
and link-local ranges, no cookies or credentials, size and time limits. Exfiltration
is then possible only to domains the user trusts. **Rejected for now:** a web search
API with arbitrary result URLs (new key and cost; model-chosen URLs would need a
prompt on every fetch). It stays a separate decision if allowlisted research proves
too narrow.

## V0.4+ (not built)

Do not add these until the previous milestone is approved:

| Version | What | Why wait |
|---|---|---|
| V0.4 | ~~SQLite tasks, sessions, facts~~ done | Need a working loop first |
| V0.5 | GitHub, files, Linux sandbox | Needs safety + confirmations |
| V0.6 | ~~Document library, courses, tutor (flashcards + /quiz)~~ done | Needs durable facts |
| V0.7 | ~~Planner, web access (D6), opportunity scout, citations rule~~ done | Needs tasks + citations rule |
| V0.8 | ~~Pending approval + resume, FastAPI + HTMX UI with token auth, dashboard~~ done (D5) | Needs data to show |
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
