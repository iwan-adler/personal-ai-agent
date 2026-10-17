"""The local web UI (V0.8): the five D5 rules, tested over real HTTP.

A real uvicorn server runs on a free 127.0.0.1 port; requests use http.client,
so Host, Origin, cookies and headers are exactly what a browser would send.
The model is a FakeLLMClient; nothing leaves the machine.
"""

import http.client
import json
import re
from pathlib import Path
import socket
import threading
import time
import urllib.parse

import pytest
import uvicorn

import app.web_ui.__main__ as web_main
from app.config import AutonomyLevel, Settings
from app.llm.client import FakeLLMClient
from app.logging_setup import setup_logging
from app.main import build_agent
from app.web_ui.security import COOKIE, Guard
from app.web_ui.server import create_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _add(title):
    return json.dumps({"action": "tool_call", "tool": "task_add", "args": {"title": title}, "summary": "Add."})


def _reply(text):
    return json.dumps({"action": "reply", "text": text, "summary": "Replied."})


class Server:
    def __init__(self, tmp_path, replies):
        self.llm = FakeLLMClient(replies=replies)
        settings = Settings(openai_api_key="k", openai_model="m", autonomy_level=AutonomyLevel.ASK_PERMISSION)
        self.agent, self.state = build_agent(settings, setup_logging("WARNING"), defer_approvals=True,
                                             data_dir=tmp_path, llm=self.llm)
        self.guard = Guard(port=_free_port())
        config = uvicorn.Config(create_app(self.agent, self.state, self.guard), host="127.0.0.1",
                                port=self.guard.port, access_log=False, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started and time.time() < deadline:
            time.sleep(0.02)
        assert self.server.started, "uvicorn did not start"

    @property
    def host(self):
        return f"127.0.0.1:{self.guard.port}"

    @property
    def origin(self):
        return f"http://{self.host}"

    def request(self, method, path, *, body=None, host=None, cookie=True, origin="same", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.guard.port, timeout=10)
        h = {"Host": host or self.host}
        if cookie:
            h["Cookie"] = f"{COOKIE}={self.guard.token}"
        if method == "POST" and origin is not None:
            h["Origin"] = self.origin if origin == "same" else origin
        if body is not None:
            body = urllib.parse.urlencode(body)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        response = conn.getresponse()
        text = response.read().decode("utf-8")
        conn.close()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, text

    def chat(self, message, **kwargs):
        return self.request("POST", "/chat", body={"message": message, "csrf": self.guard.csrf}, **kwargs)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=10)


@pytest.fixture
def make_server(tmp_path):
    servers = []

    def make(replies=()):
        server = Server(tmp_path, list(replies))
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.stop()


# --- Rule 2: token, Host, Origin, CSRF -------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:1", "localhost.evil.example:8765", None])
def test_wrong_host_is_refused(make_server, host):
    """Security: DNS rebinding makes a foreign name point at 127.0.0.1; the Host check stops it."""
    server = make_server()
    status, headers, _ = server.request("GET", "/", host=host or "rebind.attacker.example")
    assert status == 400
    assert "content-security-policy" in headers  # security headers on errors too


def test_without_the_session_token_nothing_works(make_server):
    server = make_server([_reply("should not run")])
    assert server.request("GET", "/", cookie=False)[0] == 403
    assert server.chat("hi", cookie=False)[0] == 403
    assert server.llm.calls == []


def test_the_terminal_link_signs_in_and_drops_the_token_from_the_url(make_server):
    server = make_server()
    assert server.request("GET", "/?token=guess", cookie=False)[0] == 403
    status, headers, _ = server.request("GET", f"/?token={server.guard.token}", cookie=False)
    assert status == 303 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert f"{COOKIE}={server.guard.token}" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie


@pytest.mark.parametrize(
    ("origin", "extra"),
    [(None, {}), ("http://evil.example", {}), ("null", {}),
     ("same", {"Sec-Fetch-Site": "cross-site"}), ("same", {"Sec-Fetch-Site": "same-site"})],
)
def test_cross_site_posts_are_refused(make_server, origin, extra):
    """Security: a page on another site cannot submit to the agent, even with the cookie."""
    server = make_server([_reply("should not run")])
    status, _, _ = server.chat("delete everything", origin=origin, headers=extra)
    assert status == 403
    assert server.llm.calls == []


def test_csrf_value_is_required(make_server):
    server = make_server([_reply("should not run")])
    status, _, _ = server.request("POST", "/chat", body={"message": "hi", "csrf": "wrong"})
    assert status == 403 and server.llm.calls == []


# --- Rule 4: escaping and headers -----------------------------------------------------------------


def test_page_has_strict_headers_and_safe_htmx_config(make_server):
    server = make_server()
    status, headers, body = server.request("GET", "/")
    assert status == 200
    csp = headers["content-security-policy"]
    assert "script-src 'self'" in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert headers["x-frame-options"] == "DENY" and headers["x-content-type-options"] == "nosniff"
    assert '"allowEval": false' in body and '"allowScriptTags": false' in body
    assert server.guard.csrf in body


def test_model_text_is_always_escaped(make_server):
    """Security: a reply cannot inject a script or a fake approve button."""
    evil = '<script>alert(1)</script><img src=x onerror=alert(2)><button hx-post="/approve">Approve</button>'
    server = make_server([_reply(evil)])
    status, _, body = server.chat("<b>hi</b>")
    assert status == 200
    assert "<script>alert(1)" not in body and "<img src=x" not in body and '<button hx-post="/approve">' not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body and "&lt;b&gt;hi&lt;/b&gt;" in body
    # And again from the saved history on a full page load.
    page = server.request("GET", "/")[2]
    assert "<script>alert(1)" not in page and "&lt;script&gt;" in page


def test_static_files_need_no_token_but_still_the_host_check(make_server):
    server = make_server()
    status, headers, body = server.request("GET", "/static/htmx.min.js", cookie=False)
    assert status == 200 and "javascript" in headers["content-type"] and body.startswith("var htmx")
    assert server.request("GET", "/static/htmx.min.js", cookie=False, host="evil.example")[0] == 400


# --- Rule 3: approvals --------------------------------------------------------------------------------


def _approval_fields(body):
    approval_id = re.search(r'name="approval_id" value="([^"]+)"', body).group(1)
    nonce = re.search(r'name="nonce" value="([^"]+)"', body).group(1)
    return approval_id, nonce


def test_approve_continues_the_run_and_saves_the_session(make_server):
    server = make_server([_add("Submit DBMS"), _reply("Added Submit DBMS.")])
    status, _, body = server.chat("remind me to submit DBMS")
    assert status == 200 and "Approval needed: <strong>task_add</strong>" in body
    assert server.state.tasks.list() == []
    approval_id, nonce = _approval_fields(body)
    status, _, body = server.request("POST", "/approve", body={"approval_id": approval_id, "nonce": nonce,
                                                               "answer": "approve", "csrf": server.guard.csrf})
    assert status == 200 and "Added Submit DBMS." in body
    assert [t.title for t in server.state.tasks.list()] == ["Submit DBMS"]
    saved = server.state.sessions.messages(server.state.session_id)
    assert saved == [("user", "remind me to submit DBMS"), ("assistant", "Added Submit DBMS.")]


def test_decline(make_server):
    server = make_server([_add("x"), _reply("never")])
    approval_id, nonce = _approval_fields(server.chat("add x")[2])
    body = server.request("POST", "/approve", body={"approval_id": approval_id, "nonce": nonce,
                                                    "answer": "decline", "csrf": server.guard.csrf})[2]
    assert "Not approved: tool &#39;task_add&#39;" in body
    assert server.state.tasks.list() == []


def test_a_forged_or_replayed_approval_does_nothing(make_server):
    """Security: only the id plus its one-time value, once."""
    server = make_server([_add("x"), _reply("done")])
    approval_id, nonce = _approval_fields(server.chat("add x")[2])
    forged = server.request("POST", "/approve", body={"approval_id": approval_id, "nonce": "guess",
                                                      "answer": "approve", "csrf": server.guard.csrf})[2]
    assert "no longer valid" in forged and server.state.tasks.list() == []
    ok = {"approval_id": approval_id, "nonce": nonce, "answer": "approve", "csrf": server.guard.csrf}
    assert "done" in server.request("POST", "/approve", body=ok)[2]
    assert "no longer valid" in server.request("POST", "/approve", body=ok)[2]
    assert len(server.state.tasks.list()) == 1


def test_pending_approvals_survive_a_page_reload(make_server):
    server = make_server([_add("x"), _reply("done")])
    approval_id, _ = _approval_fields(server.chat("add x")[2])
    page = server.request("GET", "/")[2]
    assert f'id="approval-{approval_id}"' in page


def test_bad_answers_and_long_messages(make_server):
    server = make_server()
    assert server.request("POST", "/approve", body={"approval_id": "a", "nonce": "b", "answer": "maybe",
                                                    "csrf": server.guard.csrf})[0] == 400
    assert "limited to 4000 characters" in server.chat("x" * 4001)[2]


# --- Rule 1: localhost only ---------------------------------------------------------------------------


def test_the_server_only_listens_on_localhost(monkeypatch, tmp_path, capsys):
    calls = {}
    monkeypatch.setattr(web_main, "load_settings", lambda: Settings(openai_api_key="k", openai_model="m"))
    monkeypatch.setattr(web_main, "build_agent",
                        lambda settings, logger, defer_approvals: build_agent(
                            settings, logger, defer_approvals=defer_approvals, data_dir=tmp_path,
                            llm=FakeLLMClient()))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.update(kw))
    assert web_main.main([]) == 0
    assert calls["host"] == "127.0.0.1" and calls["access_log"] is False and calls["proxy_headers"] is False
    out = capsys.readouterr().out
    assert re.search(r"http://127\.0\.0\.1:8765/\?token=\S{40,}", out)


# --- Dashboard (read-only) --------------------------------------------------------------------------


def test_dashboard_shows_plan_tasks_courses_cards_and_opportunities(make_server):
    from datetime import date, timedelta

    from app.memory.cards import NewCard

    server = make_server()
    state = server.state
    dbms = state.courses.add("DBMS", "Database Systems", "WS 2026/27")
    state.tasks.add("DBMS exam", date.today() + timedelta(days=4), dbms.id, estimate_hours=30)
    state.cards.add_many([NewCard("Q", "A")])
    state.opportunities.add_if_new("https://jobs.example.org/1", "Werkstudent Python", "x",
                                   "https://jobs.example.org/rss", None, ["python"])
    status, headers, body = server.request("GET", "/dashboard")
    assert status == 200 and "script-src 'self'" in headers["content-security-policy"]
    assert "DBMS exam" in body and "Not enough time" in body  # the plan's warning
    assert "Database Systems" in body and "1 due today" in body
    assert '<a href="https://jobs.example.org/1" rel="noopener noreferrer nofollow">Werkstudent Python</a>' in body
    # Since the study-partner upgrade the dashboard edits tasks: every form carries the CSRF value.
    assert body.count("<form") == body.count(f'name="csrf" value="{server.guard.csrf}"')


def test_dashboard_escapes_everything_and_needs_the_token(make_server):
    server = make_server()
    server.state.tasks.add('<script>alert("task")</script>')
    server.state.opportunities.add_if_new("https://jobs.example.org/x", '<img src=x onerror=alert(1)>', "",
                                          "https://jobs.example.org/rss", None, [])
    body = server.request("GET", "/dashboard")[2]
    assert "<script>alert" not in body and "<img src=x" not in body
    assert "&lt;script&gt;" in body and "&lt;img src=x" in body
    assert server.request("GET", "/dashboard", cookie=False)[0] == 403
    assert server.request("GET", "/dashboard", host="evil.example")[0] == 400


# --- The chat window (app.js, new chat) ---------------------------------------------------------------


def test_new_chat_starts_an_empty_session(make_server):
    server = make_server([_reply("first answer")])
    server.chat("hello")
    assert server.state.session_id is not None
    assert server.request("POST", "/new", body={"csrf": "wrong"})[0] == 403
    status, headers, _ = server.request("POST", "/new", body={"csrf": server.guard.csrf})
    assert status == 204 and headers["hx-refresh"] == "true"
    assert server.state.session_id is None and "first answer" not in server.request("GET", "/")[2]


def test_pages_show_the_model_and_load_only_own_scripts(make_server):
    server = make_server()
    body = server.request("GET", "/")[2]
    assert "Model: " in body
    assert re.findall(r"<script[^>]*src=\"([^\"]+)\"", body) == ["/static/htmx.min.js", "/static/app.js"]
    assert server.request("GET", "/static/app.js")[0] == 200


def test_templates_have_no_inline_code_or_styles():
    """Security: the CSP blocks inline scripts, handlers and styles; keep templates free of them."""
    templates = Path(web_main.__file__).parent / "templates"
    for path in templates.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path.name
        assert not re.search(r"\son[a-z]+\s*=", text), path.name
        assert " style=" not in text, path.name


def test_app_js_never_builds_html_from_text():
    """Security: messages are added with textContent only."""
    script = (Path(web_main.__file__).parents[1] / "web" / "static" / "app.js").read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in script, sink


def test_answers_are_rendered_as_markdown_safely(make_server):
    server = make_server([_reply("## Plan\n- **read** chapter 4\n<script>alert(1)</script>")])
    body = server.chat("plan")[2]
    assert "<h4>Plan</h4>" in body and "<strong>read</strong>" in body
    assert "<script>alert" not in body and "&lt;script&gt;" in body
    assert 'class="followups"' in body  # Explain more / Give an example / Test my understanding


def test_chat_history_open_and_delete(make_server):
    server = make_server([_reply("first answer"), _reply("second answer")])
    server.chat("first question")
    first = server.state.session_id
    server.request("POST", "/new", body={"csrf": server.guard.csrf})
    server.chat("second question")
    page = server.request("GET", "/")[2]
    assert f'href="/c/{first}"' in page and "first question" in page  # listed in the sidebar
    status, headers, _ = server.request("GET", f"/c/{first}")
    assert status == 303 and server.state.session_id == first
    assert "first answer" in server.request("GET", "/")[2]
    assert server.request("POST", f"/c/{first}/delete", body={"csrf": "wrong"})[0] == 403
    status, headers, _ = server.request("POST", f"/c/{first}/delete", body={"csrf": server.guard.csrf})
    assert status == 204 and headers["hx-redirect"] == "/"
    assert server.state.session_id is None and f'href="/c/{first}"' not in server.request("GET", "/")[2]
    assert server.request("POST", f"/c/{first}/delete", body={"csrf": server.guard.csrf})[0] == 404


# --- Dashboard task controls --------------------------------------------------------------------------


def _post(server, path, **body):
    return server.request("POST", path, body={"csrf": server.guard.csrf, **body})


def test_dashboard_creates_edits_completes_and_deletes_tasks(make_server):
    from datetime import date, timedelta

    server = make_server()
    course = server.state.courses.add("DBMS", "Database Systems", None)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    status, headers, body = _post(server, "/tasks", title="Read chapter 4", due=tomorrow, course=str(course.id), estimate="2.5")
    assert status == 200 and "Read chapter 4" in body and json.loads(headers["hx-trigger"]) == {"toast": "Task added."}
    task = server.state.tasks.list()[0]
    assert (task.due_date, task.course, task.estimate_hours) == (tomorrow, "DBMS", 2.5)

    assert _post(server, f"/tasks/{task.id}/edit", title="Read chapter 5", due="", course="", estimate="")[0] == 200
    task = server.state.tasks.get(task.id)
    assert (task.title, task.due_date, task.course_id, task.estimate_hours) == ("Read chapter 5", None, None, None)

    assert _post(server, f"/tasks/{task.id}/done")[0] == 200 and server.state.tasks.get(task.id).status == "done"
    assert _post(server, f"/tasks/{task.id}/done")[0] == 400  # already done
    assert _post(server, f"/tasks/{task.id}/reopen")[0] == 200 and server.state.tasks.get(task.id).status == "open"
    assert _post(server, f"/tasks/{task.id}/delete")[0] == 200 and server.state.tasks.list("all") == []
    assert _post(server, f"/tasks/{task.id}/delete")[0] == 404


@pytest.mark.parametrize("fields", [
    {"title": ""}, {"title": "x" * 201}, {"title": "ok", "due": "next friday"}, {"title": "ok", "due": "1900-01-01"},
    {"title": "ok", "course": "999"}, {"title": "ok", "course": "abc"}, {"title": "ok", "estimate": "0"},
    {"title": "ok", "estimate": "1e9"}, {"title": "ok", "estimate": "lots"},
])
def test_dashboard_rejects_bad_task_values(make_server, fields):
    server = make_server()
    status, _, text = _post(server, "/tasks", **{"due": "", "course": "", "estimate": "", **fields})
    assert status == 400 and text and server.state.tasks.list("all") == []


def test_dashboard_changes_need_csrf_and_same_origin(make_server):
    """Security: another website cannot add or delete your tasks."""
    server = make_server()
    task = server.state.tasks.add("keep me")
    assert server.request("POST", "/tasks", body={"title": "x", "csrf": "wrong"})[0] == 403
    assert server.request("POST", f"/tasks/{task.id}/delete", body={"csrf": "wrong"})[0] == 403
    assert server.request("POST", f"/tasks/{task.id}/delete", body={"csrf": server.guard.csrf},
                          origin="http://evil.example")[0] == 403
    assert server.request("POST", f"/tasks/{task.id}/explode", body={"csrf": server.guard.csrf})[0] == 404
    assert [t.title for t in server.state.tasks.list()] == ["keep me"]


def test_dashboard_logs_ids_not_titles(make_server, tmp_path):
    server = make_server()
    _post(server, "/tasks", title="Secret project name", due="", course="", estimate="")
    log = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"task_ui_add"' in log and "Secret project name" not in log


def test_welcome_name_and_study_time(make_server):
    server = make_server()
    assert "Set your name" in server.request("GET", "/dashboard")[2]
    body = _post(server, "/prefs/name", name="  Ana  ")[2]
    assert ", Ana 👋" in body
    assert _post(server, "/prefs/name", name="x" * 41)[0] == 400
    assert "<script>" not in _post(server, "/prefs/name", name="<script>x</script>")[2]
