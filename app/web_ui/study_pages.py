"""Study Mode pages: choose a topic, then learn with the tutor (app/tutor.py).

The tutor has no tools, so these routes can only show and store what was said;
the usual guards (token cookie, Host, Origin, CSRF) apply as everywhere.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.llm.client import LLMError
from app.main import ChatState
from app.memory.courses import CourseNotFound
from app.memory.study import DIFFICULTIES, MODES, StudyNotFound
from app.tutor import ACTIONS, Tutor, take_turn

MAX_SUBJECT = 80
MAX_TOPIC = 200
MAX_GOAL = 300
MAX_LINKED_DOCUMENTS = 20


def add_study_routes(app: FastAPI, *, state: ChatState, tutor: Tutor, render: Callable[..., HTMLResponse],
                     csrf_ok: Callable[[str], bool], plain: Callable[[int, str], Response],
                     secure: Callable[[Response], Response]) -> None:
    study = state.study

    def courses() -> list:
        return [s.course for s in state.courses.list()] if state.courses else []

    @app.get("/study", response_class=HTMLResponse)
    def study_home(request: Request, document: int | None = None):
        documents = state.docs.list() if state.docs else []
        chosen = next((d for d in documents if d.id == document), None)
        prefill = {}
        if chosen is not None:  # "Study this" on the Materials page
            stem = chosen.name.rsplit(".", 1)[0].replace("_", " ").strip()
            prefill = {"subject": chosen.course or "", "topic": stem[:MAX_TOPIC]}
        return render(request, "study.html", page="study", sessions=study.recent(30), courses=courses(),
                      difficulties=DIFFICULTIES, modes=MODES, documents=documents,
                      chosen=chosen.id if chosen else None, prefill=prefill)

    @app.post("/study/start")
    async def study_start(request: Request):
        form = await request.form(max_fields=MAX_LINKED_DOCUMENTS + 10)
        if not csrf_ok(str(form.get("csrf", ""))):
            return plain(403, "Missing or wrong CSRF value.")
        subject, topic, goal = (str(form.get(k, "")) for k in ("subject", "topic", "goal"))
        difficulty, mode = str(form.get("difficulty", "beginner")), str(form.get("mode", "learn"))
        try:
            document_ids = [int(v) for v in form.getlist("documents")][:MAX_LINKED_DOCUMENTS]
        except ValueError:
            return plain(400, "Unknown file.")
        subject, topic, goal = subject.strip(), topic.strip(), goal.strip()
        if not subject or not topic:
            return plain(400, "Choose a subject and a topic.")
        if len(subject) > MAX_SUBJECT or len(topic) > MAX_TOPIC or len(goal) > MAX_GOAL:
            return plain(400, "Subject, topic or goal is too long.")
        if difficulty not in DIFFICULTIES or mode not in MODES:
            return plain(400, "Unknown difficulty or mode.")
        course_id = None
        if state.courses:
            try:
                course_id = state.courses.get(subject).id  # the subject is one of your course codes
            except (CourseNotFound, ValueError):
                course_id = None
        session = study.start(subject, topic, difficulty, goal, mode, course_id)
        study.link_documents(session.id, document_ids)
        state.audit_event("study_start", session.id)
        try:
            take_turn(study, tutor, session.id, "start", docs=state.docs)
        except LLMError:
            pass  # the session page offers "Start" again
        return secure(Response(status_code=204, headers={"HX-Redirect": f"/study/{session.id}"}))

    @app.get("/study/{session_id}", response_class=HTMLResponse)
    def study_session(request: Request, session_id: int):
        try:
            session = study.get(session_id)
        except StudyNotFound:
            return plain(404, "That study session no longer exists.")
        linked = [d for d in (state.docs.list() if state.docs else []) if d.id in set(study.document_ids(session_id))]
        return render(request, "study_session.html", page="study", s=session, linked=linked,
                      messages=study.messages(session_id), modes=MODES)

    @app.post("/study/{session_id}/turn", response_class=HTMLResponse)
    def study_turn(request: Request, session_id: int, action: str = Form(""), text: str = Form(""),
                   mode: str = Form(""), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        if action not in ACTIONS or (action == "mode" and mode not in MODES):
            return plain(400, "Unknown action.")
        try:
            turn, session = take_turn(study, tutor, session_id, action, text, new_mode=mode or None,
                                      docs=state.docs)
        except StudyNotFound:
            return plain(404, "That study session no longer exists.")
        except ValueError as exc:
            return render(request, "_study_turn.html", s=study.get(session_id), error=str(exc), turn=None,
                          modes=MODES)
        except LLMError as exc:
            return render(request, "_study_turn.html", s=study.get(session_id), error=str(exc), turn=None,
                          modes=MODES, student_text=text.strip() or None)
        return render(request, "_study_turn.html", s=session, turn=turn, error=None, modes=MODES)

    @app.post("/study/{session_id}/delete")
    def study_delete(session_id: int, csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        try:
            study.delete(session_id)
        except StudyNotFound:
            return plain(404, "That study session no longer exists.")
        state.audit_event("study_delete", session_id)
        return secure(Response(status_code=204, headers={"HX-Redirect": "/study"}))
