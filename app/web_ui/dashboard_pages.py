"""The dashboard: today, deadlines, study time, subject progress and task editing.

Since the study-partner upgrade (D5 update), the dashboard can change tasks
directly: create, edit, complete, reopen and delete. These are *your* clicks,
not model proposals, so they do not go through the agent's approval step;
they get the same protection as every other form (session cookie, Host,
Origin and CSRF checks) and are written to the audit log by id only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, time, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.main import ChatState
from app.memory.tasks import TaskAlreadyDone, TaskNotFound
from app.planner import WEEKDAYS
from app.tools.planner import compute_plan

PLAN_DAYS = 7
DEFAULT_HOURS = 2.0  # what the planner assumes without any setting
UPCOMING_DAYS = 14
MAX_TITLE = 200
MAX_NAME = 40


def _greeting(hour: int) -> str:
    if hour < 5:
        return "Working late"
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def _local_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min).astimezone()


class FormError(ValueError):
    """A dashboard form value is not acceptable; the message is shown to you."""


def _parse_task_form(state: ChatState, title: str, due: str, course: str, estimate: str):
    title = " ".join(title.split())
    if not title or len(title) > MAX_TITLE:
        raise FormError(f"A task needs a title of 1-{MAX_TITLE} characters.")
    due_date = None
    if due.strip():
        try:
            due_date = date.fromisoformat(due.strip())
        except ValueError:
            raise FormError("The due date is not a valid date.") from None
        if not 2000 <= due_date.year <= 2100:
            raise FormError("The due date is not a valid date.")
    course_id = None
    if course.strip():
        try:
            course_id = int(course)
        except ValueError:
            raise FormError("Unknown course.") from None
        if state.courses is None or course_id not in {s.course.id for s in state.courses.list()}:
            raise FormError("Unknown course.")
    hours = None
    if estimate.strip():
        try:
            hours = float(estimate)
        except ValueError:
            raise FormError("The estimate must be a number of hours.") from None
        if not 0 < hours <= 200:
            raise FormError("The estimate must be between 0 and 200 hours.")
    return title, due_date, course_id, hours


def dashboard_context(state: ChatState, today: date | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now().astimezone()
    today = today or now.date()
    tasks = state.tasks.list(status="open", limit=500) if state.tasks else []
    groups = {"overdue": [], "today": [], "upcoming": [], "later": []}
    for task in tasks:
        due = date.fromisoformat(task.due_date) if task.due_date else None
        if due is None or due > today + timedelta(days=UPCOMING_DAYS):
            groups["later"].append(task)
        elif due < today:
            groups["overdue"].append(task)
        elif due == today:
            groups["today"].append(task)
        else:
            groups["upcoming"].append(task)

    study_today = study_week = 0
    by_subject: dict[str, int] = {}
    if state.study:
        week_start = today - timedelta(days=today.weekday())
        study_today = state.study.seconds_since(_local_midnight(today))
        study_week = state.study.seconds_since(_local_midnight(week_start))
        by_subject = state.study.seconds_by_subject()

    cards = state.cards.stats(today=today) if state.cards else None
    card_counts = {code: (total, due) for code, total, due in cards.by_course} if cards else {}
    progress_by_course = state.tasks.progress_by_course() if state.tasks else {}
    subjects = []
    courses = state.courses.list() if state.courses else []
    for stats in courses:
        course = stats.course
        done, total = progress_by_course.get(course.id, (0, 0))
        card_total, card_due = card_counts.get(course.code, (0, 0))
        subjects.append({
            "code": course.code, "name": course.name, "done": done, "total": total,
            "percent": round(100 * done / total) if total else 0, "cards_due": card_due, "cards": card_total,
            "minutes": round(by_subject.pop(course.code, 0) / 60), "next_due": stats.next_due_date,
        })
    for subject, seconds in by_subject.items():  # studied subjects that are not courses
        subjects.append({"code": subject, "name": "", "done": 0, "total": 0, "percent": 0, "cards_due": 0,
                         "cards": 0, "minutes": round(seconds / 60), "next_due": None})

    plan = compute_plan(state.tasks, state.cards, state.current_study_hours(), PLAN_DAYS) if state.tasks else None
    return {
        "greeting": _greeting(now.hour),
        "user_name": state.prefs.get("name") if state.prefs else None,
        "today": today,
        "groups": groups,
        "open_count": len(tasks),
        "done_recent": state.tasks.recently_done(5) if state.tasks else [],
        "courses": [s.course for s in courses],
        "subjects": subjects,
        "study_today_min": round(study_today / 60),
        "study_week_min": round(study_week / 60),
        "plan": plan,
        "cards": cards,
        "opportunities": state.opportunities.list("new", 20) if state.opportunities else [],
        "recent_study": state.study.recent(3) if state.study else [],
        "hours": list(zip(WEEKDAYS, state.current_study_hours() or (DEFAULT_HOURS,) * 7)),
        "hours_source": state.study_hours_source(),
        "classes_today": state.timetable.day(today.weekday()) if state.timetable else [],
        "has_timetable": bool(state.timetable.week()) if state.timetable else False,
        "now_time": now.strftime("%H:%M"),
        "env_hours": state.study_hours is not None,
        "hours_total": sum(state.current_study_hours() or (DEFAULT_HOURS,) * 7),
    }


def add_dashboard_routes(app: FastAPI, *, state: ChatState, render: Callable[..., HTMLResponse],
                         csrf_ok: Callable[[str], bool], plain: Callable[[int, str], Response]) -> None:

    def audit(event: str, **fields) -> None:
        try:
            state.audit.write(event, **fields)  # ids only, never titles
        except OSError:
            pass

    def body(request: Request, message: str) -> HTMLResponse:
        response = render(request, "_dash_body.html", **dashboard_context(state))
        response.headers["HX-Trigger"] = json.dumps({"toast": message})
        return response

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        return render(request, "dashboard.html", page="dashboard", **dashboard_context(state))

    @app.post("/tasks", response_class=HTMLResponse)
    def task_add(request: Request, title: str = Form(""), due: str = Form(""), course: str = Form(""),
                 estimate: str = Form(""), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        if state.tasks is None:
            return plain(400, "Tasks are not configured.")
        try:
            title, due_date, course_id, hours = _parse_task_form(state, title, due, course, estimate)
        except FormError as exc:
            return plain(400, str(exc))
        task = state.tasks.add(title, due_date, course_id, hours)
        audit("task_ui_add", task_id=task.id)
        return body(request, "Task added.")

    @app.post("/tasks/{task_id}/edit", response_class=HTMLResponse)
    def task_edit(request: Request, task_id: int, title: str = Form(""), due: str = Form(""),
                  course: str = Form(""), estimate: str = Form(""), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        try:
            title, due_date, course_id, hours = _parse_task_form(state, title, due, course, estimate)
            state.tasks.update(task_id, title, due_date, course_id, hours)
        except FormError as exc:
            return plain(400, str(exc))
        except TaskNotFound:
            return plain(404, "That task no longer exists.")
        audit("task_ui_edit", task_id=task_id)
        return body(request, "Task saved.")

    @app.post("/tasks/{task_id}/{action}", response_class=HTMLResponse)
    def task_action(request: Request, task_id: int, action: str, csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        if action not in ("done", "reopen", "delete"):
            return plain(404, "Unknown action.")
        try:
            if action == "done":
                state.tasks.complete(task_id)
            elif action == "reopen":
                state.tasks.reopen(task_id)
            else:
                state.tasks.delete(task_id)
        except TaskNotFound:
            return plain(404, "That task no longer exists.")
        except TaskAlreadyDone:
            return plain(400, "That task is already done.")
        audit(f"task_ui_{action}", task_id=task_id)
        return body(request, {"done": "Nice work! Task completed.", "reopen": "Task reopened.",
                              "delete": "Task deleted."}[action])

    @app.post("/prefs/study-hours", response_class=HTMLResponse)
    async def set_study_hours(request: Request):
        form = await request.form()
        if not csrf_ok(str(form.get("csrf", ""))):
            return plain(403, "Missing or wrong CSRF value.")
        if state.prefs is None:
            return plain(400, "Settings are not available.")
        if form.get("reset") == "1":
            state.prefs.set_study_hours(None)
            return body(request, "Study hours reset.")
        hours = []
        for day in WEEKDAYS:
            raw = str(form.get(day, "")).strip() or "0"
            try:
                value = float(raw)
            except ValueError:
                return plain(400, f"{day.capitalize()}: enter a number of hours.")
            if not 0 <= value <= 16 or value != value:
                return plain(400, f"{day.capitalize()}: hours must be between 0 and 16.")
            hours.append(round(value * 4) / 4)  # quarter hours, like the planner
        state.prefs.set_study_hours(tuple(hours))
        return body(request, "Study hours saved. The plan is updated.")

    @app.post("/prefs/name", response_class=HTMLResponse)
    def set_name(request: Request, name: str = Form(""), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        name = " ".join(name.split())
        if len(name) > MAX_NAME:
            return plain(400, f"Names are limited to {MAX_NAME} characters.")
        state.prefs.set("name", name or None)
        return body(request, "Saved." if name else "Name removed.")
