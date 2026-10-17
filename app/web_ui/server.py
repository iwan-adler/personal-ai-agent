"""The local web UI (V0.8, D5): FastAPI with server-rendered HTMX.

It is just another way to say yes or no: runs go through the same AgentLoop
in deferred mode, so every safety step is the CLI's. Pages render with Jinja2
autoescape; all checks from app/web_ui/security.py run before any route.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from app.agent.loop import AgentLoop, ApprovalError, AgentRun, RunStatus
from app.llm.client import LLMError
from app.conversation import Conversation
from app.main import ChatState
from app.tools.planner import compute_plan
from app.web_ui.security import COOKIE, SECURITY_HEADERS, Guard

HERE = Path(__file__).resolve().parent
STATIC = HERE.parent / "web" / "static"
MAX_MESSAGE_CHARS = 4_000
DASHBOARD_DAYS = 7


def _templates() -> Jinja2Templates:
    # autoescape=True for every template: model and tool text can never become HTML.
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=True)
    return Jinja2Templates(env=env)


def create_app(agent: AgentLoop, state: ChatState, guard: Guard) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = _templates()
    waiting: dict[str, str] = {}  # run id -> the user's message, saved once the run ends

    @app.middleware("http")
    async def enforce_rules(request: Request, call_next):
        if not guard.host_ok(request.headers.get("host")):
            return _plain(400, "Bad Host header.")
        path = request.url.path
        if not path.startswith("/static/"):
            if request.method == "GET" and path == "/" and "token" in request.query_params:
                if not guard.token_ok(request.query_params.get("token")):
                    return _plain(403, "Wrong or expired link. Use the link printed in the terminal.")
                response = RedirectResponse("/", status_code=303)  # drop the token from the address bar
                response.set_cookie(COOKIE, guard.token, httponly=True, samesite="strict", path="/")
                return _secure(response)
            if not guard.token_ok(request.cookies.get(COOKIE)):
                return _plain(403, "Not signed in. Open the link printed in the terminal.")
            if request.method not in ("GET", "HEAD"):
                if not guard.origin_ok(request.headers.get("origin"), request.headers.get("sec-fetch-site")):
                    return _plain(403, "Cross-site request refused.")
        return _secure(await call_next(request))

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request, name, {"csrf": guard.csrf, "model": agent.settings.openai_model, **context})

    def turn(request: Request, run: AgentRun | None, user_text: str | None, error: str | None = None):
        if run is not None and run.status != RunStatus.AWAITING_APPROVAL:
            original = waiting.pop(run.run_id, user_text)
            if original is not None:
                state.record(original, run.final_text)
        elif run is not None and user_text is not None:
            waiting[run.run_id] = user_text
        return render(request, "_turn.html", run=run, user_text=user_text, error=error)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        # The saved session (your messages and final replies), not the internal
        # conversation, which also holds model JSON and tool results.
        history = state.sessions.messages(state.session_id) if state.session_id is not None else []
        return render(request, "index.html", page="chat", history=history, pending=agent.pending_approvals())

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        """Read-only overview: it never changes anything, so it has no forms."""
        plan = compute_plan(state.tasks, state.cards, state.study_hours, DASHBOARD_DAYS) if state.tasks else None
        return render(
            request,
            "dashboard.html",
            page="dashboard",
            plan=plan,
            tasks=state.tasks.list(status="open", limit=50) if state.tasks else [],
            courses=state.courses.list() if state.courses else [],
            cards=state.cards.stats() if state.cards else None,
            opportunities=state.opportunities.list("new", 20) if state.opportunities else [],
        )

    @app.post("/chat", response_class=HTMLResponse)
    def chat(request: Request, message: str = Form(""), csrf: str = Form("")):
        if not guard.csrf_ok(csrf):
            return _plain(403, "Missing or wrong CSRF value.")
        message = message.strip()
        if not message:
            return turn(request, None, None, error="Type a message first.")
        if len(message) > MAX_MESSAGE_CHARS:
            return turn(request, None, None, error=f"Messages are limited to {MAX_MESSAGE_CHARS} characters.")
        try:
            run = agent.run(message, state.conversation)
        except LLMError as exc:
            return turn(request, None, message, error=str(exc))
        return turn(request, run, message)

    @app.post("/new")
    def new_chat(csrf: str = Form("")):
        """Start a new session, like /new in the CLI; the page reloads empty."""
        if not guard.csrf_ok(csrf):
            return _plain(403, "Missing or wrong CSRF value.")
        state.conversation = Conversation()
        state.session_id = None
        return _secure(Response(status_code=204, headers={"HX-Refresh": "true"}))

    @app.post("/approve", response_class=HTMLResponse)
    def approve(request: Request, approval_id: str = Form(""), nonce: str = Form(""),
                answer: str = Form(""), csrf: str = Form("")):
        if not guard.csrf_ok(csrf):
            return _plain(403, "Missing or wrong CSRF value.")
        if answer not in ("approve", "decline"):
            return _plain(400, "Answer must be approve or decline.")
        try:
            run = agent.resume(approval_id, nonce, approved=answer == "approve")
        except ApprovalError as exc:
            return turn(request, None, None, error=f"This approval is no longer valid: {exc}")
        except LLMError as exc:
            return turn(request, None, None, error=str(exc))
        return turn(request, run, None)

    return app


def _secure(response: Response) -> Response:
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def _plain(status: int, text: str) -> Response:
    return _secure(Response(text, status_code=status, media_type="text/plain; charset=utf-8"))
