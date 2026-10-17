"""Materials (upload, organize, read with the model) and the class timetable.

Uploads are saved and imported by app/materials.py; the model is used only by
"Find deadlines" and "Read timetable" (app/extract.py), which return a list
you edit and confirm before anything is saved. Every form here has the same
guards as the rest of the UI (session cookie, Host, Origin, CSRF).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date

from fastapi import FastAPI, Form, Request
from starlette.datastructures import UploadFile  # what request.form() returns
from fastapi.responses import HTMLResponse, Response

from app.extract import ExtractError, propose_tasks, propose_timetable
from app.llm.client import Completer, LLMError
from app.main import ChatState
from app.materials import UploadError, max_bytes_for, remove_material, safe_name, save_upload
from app.memory.documents import CATEGORIES, DocumentNotFound
from app.memory.timetable import DAY_NAMES, ClassNotFound
from app.web_ui.dashboard_pages import FormError, _parse_task_form

MAX_UPLOAD_FILES = 20
MAX_CONFIRM_ROWS = 80
CATEGORY_LABELS = {"notes": "Lecture notes", "assignment": "Assignment", "timetable": "Timetable",
                   "classwork": "Classwork", "other": "Other"}


def _doc_text(state: ChatState, document_id: int) -> str:
    parts = []
    for hit in state.docs.chunks(document_id):
        where = f"[page {hit.page}]" if hit.page is not None else ""
        parts.append(f"{where}\n{hit.text}" if where else hit.text)
    return "\n\n".join(parts)


def _guess_course(state: ChatState, title: str) -> int | None:
    """A course whose code or name appears in a class title, for the review form."""
    if not state.courses:
        return None
    lowered = title.lower()
    for stats in state.courses.list():
        course = stats.course
        if course.code.lower() in lowered.split() or course.name.lower() in lowered:
            return course.id
    return None


def add_material_routes(app: FastAPI, *, state: ChatState, llm: Completer, render: Callable[..., HTMLResponse],
                        csrf_ok: Callable[[str], bool], plain: Callable[[int, str], Response]) -> None:

    def audit(event: str, **fields) -> None:
        try:
            state.audit.write(event, **fields)  # ids and counts only, never names or content
        except OSError:
            pass

    def courses() -> list:
        return [s.course for s in state.courses.list()] if state.courses else []

    def course_by_id(raw: str):
        if not raw.strip():
            return None
        try:
            wanted = int(raw)
        except ValueError:
            raise FormError("Unknown course.") from None
        for course in courses():
            if course.id == wanted:
                return course
        raise FormError("Unknown course.")

    def library_context(**extra) -> dict:
        documents = state.docs.list() if state.docs else []
        groups = {c: [d for d in documents if d.category == c] for c in CATEGORIES}
        return {"groups": groups, "labels": CATEGORY_LABELS, "courses": courses(), "count": len(documents),
                "workspace_ok": state.workspace is not None, **extra}

    def library(request: Request, message: str | None = None, **extra) -> HTMLResponse:
        response = render(request, "_materials_list.html", **library_context(**extra))
        if message:
            response.headers["HX-Trigger"] = json.dumps({"toast": message})
        return response

    @app.get("/materials", response_class=HTMLResponse)
    def materials(request: Request):
        return render(request, "materials.html", page="materials", categories=CATEGORIES, **library_context())

    @app.post("/materials/upload", response_class=HTMLResponse)
    async def upload(request: Request):
        form = await request.form(max_files=MAX_UPLOAD_FILES, max_fields=20)
        if not csrf_ok(str(form.get("csrf", ""))):
            return plain(403, "Missing or wrong CSRF value.")
        if state.workspace is None or state.docs is None:
            return plain(400, "Set WORKSPACE_DIR in .env first: uploaded files are kept there.")
        category = str(form.get("category", "notes"))
        if category not in CATEGORIES:
            return plain(400, "Unknown category.")
        try:
            course = course_by_id(str(form.get("course", "")))
        except FormError as exc:
            return plain(400, str(exc))
        files = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        if not files:
            return plain(400, "Choose at least one file.")
        if len(files) > MAX_UPLOAD_FILES:
            return plain(400, f"Upload at most {MAX_UPLOAD_FILES} files at once.")
        results = []
        for file in files:
            try:
                limit = max_bytes_for(safe_name(file.filename))
                data = await file.read(limit + 1)
                uploaded = save_upload(state.workspace, state.docs, file.filename, data,
                                       course.code if course else None, course.id if course else None, category)
                results.append((uploaded.document.name, uploaded.status, None))
                audit("material_upload", document_id=uploaded.document.id, status=uploaded.status)
            except UploadError as exc:
                results.append((file.filename[:100], "refused", str(exc)))
            finally:
                await file.close()
        ok = sum(1 for r in results if r[2] is None)
        message = f"{ok} file{'s' if ok != 1 else ''} added." if ok else "No file was added."
        return library(request, message, results=results)

    @app.post("/materials/{document_id}/edit", response_class=HTMLResponse)
    def edit(request: Request, document_id: int, category: str = Form(""), course: str = Form(""),
             csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        if category not in CATEGORIES:
            return plain(400, "Unknown category.")
        try:
            chosen = course_by_id(course)
            state.docs.set_category(document_id, category)
            state.docs.set_course(document_id, chosen.id if chosen else None)
        except FormError as exc:
            return plain(400, str(exc))
        except DocumentNotFound:
            return plain(404, "That file is no longer in your materials.")
        return library(request, "Saved.")

    @app.post("/materials/{document_id}/remove", response_class=HTMLResponse)
    def remove(request: Request, document_id: int, csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        if state.workspace is None:
            return plain(400, "WORKSPACE_DIR is not set.")
        try:
            name = remove_material(state.workspace, state.docs, document_id)
        except DocumentNotFound:
            return plain(404, "That file is no longer in your materials.")
        audit("material_remove", document_id=document_id)
        return library(request, f"Removed {name}.")

    # --- Reading files with the model: you review before anything is saved ---------------------------

    @app.post("/materials/{document_id}/deadlines", response_class=HTMLResponse)
    def find_deadlines(request: Request, document_id: int, csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        try:
            document = state.docs.get(document_id)
            proposal = propose_tasks(llm, _doc_text(state, document_id), document.course)
        except DocumentNotFound:
            return plain(404, "That file is no longer in your materials.")
        except (ExtractError, LLMError) as exc:
            return plain(502, str(exc))
        audit("material_read_deadlines", document_id=document_id, proposed=len(proposal.items))
        return render(request, "_review_tasks.html", document=document, proposal=proposal,
                      courses=courses(), today=date.today())

    @app.post("/materials/tasks/confirm", response_class=HTMLResponse)
    async def confirm_tasks(request: Request):
        form = await request.form(max_fields=MAX_CONFIRM_ROWS * 6 + 5)
        if not csrf_ok(str(form.get("csrf", ""))):
            return plain(403, "Missing or wrong CSRF value.")
        if state.tasks is None:
            return plain(400, "Tasks are not configured.")
        rows = []
        for i in range(MAX_CONFIRM_ROWS):
            if form.get(f"pick_{i}") != "1":
                continue
            try:
                rows.append(_parse_task_form(state, str(form.get(f"title_{i}", "")), str(form.get(f"due_{i}", "")),
                                             str(form.get(f"course_{i}", "")), str(form.get(f"estimate_{i}", ""))))
            except FormError as exc:
                return plain(400, f"Row {i + 1}: {exc}")
        if not rows:
            return plain(400, "Tick at least one task to add.")
        for title, due, course_id, hours in rows:
            task = state.tasks.add(title, due, course_id, hours)
            audit("task_ui_add", task_id=task.id)
        response = render(request, "_review_done.html", text=f"Added {len(rows)} task{'s' if len(rows) != 1 else ''}.",
                          link="/dashboard", link_text="See them on the dashboard")
        response.headers["HX-Trigger"] = json.dumps({"toast": "Tasks added."})
        return response

    @app.post("/materials/{document_id}/timetable", response_class=HTMLResponse)
    def read_timetable(request: Request, document_id: int, csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        try:
            document = state.docs.get(document_id)
            proposal = propose_timetable(llm, _doc_text(state, document_id))
        except DocumentNotFound:
            return plain(404, "That file is no longer in your materials.")
        except (ExtractError, LLMError) as exc:
            return plain(502, str(exc))
        audit("material_read_timetable", document_id=document_id, proposed=len(proposal.items))
        guesses = [_guess_course(state, item.title) for item in proposal.items]
        return render(request, "_review_timetable.html", document=document, proposal=proposal,
                      rows=list(zip(proposal.items, guesses)), courses=courses(), days=DAY_NAMES,
                      existing=len(state.timetable.week()))

    @app.post("/timetable/confirm", response_class=HTMLResponse)
    async def confirm_timetable(request: Request):
        form = await request.form(max_fields=MAX_CONFIRM_ROWS * 8 + 5)
        if not csrf_ok(str(form.get("csrf", ""))):
            return plain(403, "Missing or wrong CSRF value.")
        rows = []
        for i in range(MAX_CONFIRM_ROWS):
            if form.get(f"pick_{i}") != "1":
                continue
            try:
                course = course_by_id(str(form.get(f"course_{i}", "")))
                weekday = int(str(form.get(f"day_{i}", "-1")))
                rows.append((weekday, str(form.get(f"start_{i}", "")), str(form.get(f"end_{i}", "")),
                             str(form.get(f"title_{i}", "")), str(form.get(f"room_{i}", "")), course.id if course else None))
            except (FormError, ValueError) as exc:
                return plain(400, f"Row {i + 1}: {exc}")
        if not rows:
            return plain(400, "Tick at least one class to save.")
        from app.memory.timetable import check_slot

        for weekday, start, end, title, room, _ in rows:
            try:
                check_slot(weekday, start, end, title, room)
            except ValueError as exc:
                return plain(400, f"{title or 'A class'}: {exc}")
        if form.get("replace") == "1":
            state.timetable.clear()
        for weekday, start, end, title, room, course_id in rows:
            state.timetable.add(weekday, start, end, title, room, course_id)
        audit("timetable_save", classes=len(rows))
        response = render(request, "_review_done.html", text=f"Saved {len(rows)} class{'es' if len(rows) != 1 else ''}.",
                          link="/timetable", link_text="Open your timetable")
        response.headers["HX-Trigger"] = json.dumps({"toast": "Timetable saved."})
        return response

    # --- The timetable page ---------------------------------------------------------------------------

    def week_context() -> dict:
        slots = state.timetable.week()
        return {"days": DAY_NAMES, "by_day": [[s for s in slots if s.weekday == d] for d in range(7)],
                "courses": courses(), "today_index": date.today().weekday(), "total": len(slots),
                "hours": round(sum(s.hours for s in slots), 2)}

    def week(request: Request, message: str) -> HTMLResponse:
        response = render(request, "_timetable_week.html", **week_context())
        response.headers["HX-Trigger"] = json.dumps({"toast": message})
        return response

    @app.get("/timetable", response_class=HTMLResponse)
    def timetable(request: Request):
        return render(request, "timetable.html", page="timetable", **week_context())

    @app.post("/timetable", response_class=HTMLResponse)
    def add_class(request: Request, day: str = Form(""), start: str = Form(""), end: str = Form(""),
                  title: str = Form(""), room: str = Form(""), course: str = Form(""), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        try:
            chosen = course_by_id(course)
            state.timetable.add(int(day) if day.isdigit() else -1, start, end, title, room, chosen.id if chosen else None)
        except (FormError, ValueError) as exc:
            return plain(400, str(exc))
        return week(request, "Class added.")

    @app.post("/timetable/{slot_id}/edit", response_class=HTMLResponse)
    def edit_class(request: Request, slot_id: int, day: str = Form(""), start: str = Form(""), end: str = Form(""),
                   title: str = Form(""), room: str = Form(""), course: str = Form(""), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        try:
            chosen = course_by_id(course)
            state.timetable.update(slot_id, int(day) if day.isdigit() else -1, start, end, title, room,
                                   chosen.id if chosen else None)
        except ClassNotFound:
            return plain(404, "That class no longer exists.")
        except (FormError, ValueError) as exc:
            return plain(400, str(exc))
        return week(request, "Class saved.")

    @app.post("/timetable/{slot_id}/delete", response_class=HTMLResponse)
    def delete_class(request: Request, slot_id: int, csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        try:
            state.timetable.delete(slot_id)
        except ClassNotFound:
            return plain(404, "That class no longer exists.")
        return week(request, "Class removed.")
