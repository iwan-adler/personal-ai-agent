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


def add_study_routes(app: FastAPI, *, state: ChatState, tutor: Tutor, render: Callable[..., HTMLResponse],
                     csrf_ok: Callable[[str], bool], plain: Callable[[int, str], Response],
                     secure: Callable[[Response], Response]) -> None:
    study = state.study

    def courses() -> list:
        return [s.course for s in state.courses.list()] if state.courses else []

    @app.get("/study", response_class=HTMLResponse)
    def study_home(request: Request):
        return render(request, "study.html", page="study", sessions=study.recent(30), courses=courses(),
                      difficulties=DIFFICULTIES, modes=MODES)

    @app.post("/study/start")
    def study_start(subject: str = Form(""), topic: str = Form(""), difficulty: str = Form("beginner"),
                    goal: str = Form(""), mode: str = Form("learn"), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
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
        state.audit_event("study_start", session.id)
        try:
            take_turn(study, tutor, session.id, "start")
        except LLMError:
            pass  # the session page offers "Start" again
        return secure(Response(status_code=204, headers={"HX-Redirect": f"/study/{session.id}"}))

    @app.get("/study/{session_id}", response_class=HTMLResponse)
    def study_session(request: Request, session_id: int):
        try:
            session = study.get(session_id)
        except StudyNotFound:
            return plain(404, "That study session no longer exists.")
        return render(request, "study_session.html", page="study", s=session,
                      messages=study.messages(session_id), modes=MODES)

    @app.post("/study/{session_id}/turn", response_class=HTMLResponse)
    def study_turn(request: Request, session_id: int, action: str = Form(""), text: str = Form(""),
                   mode: str = Form(""), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        if action not in ACTIONS or (action == "mode" and mode not in MODES):
            return plain(400, "Unknown action.")
        try:
            turn, session = take_turn(study, tutor, session_id, action, text, new_mode=mode or None)
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
