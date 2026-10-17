"""Materials: uploads, reading files with the model (you confirm), timetable, personalized lessons."""

import json
import uuid
from datetime import date, timedelta

import pytest

from app.extract import ProposedClass, propose_tasks, propose_timetable
from app.llm.client import FakeLLMClient
from app.materials import UploadError, safe_name, save_upload
from app.memory.documents import DocumentStore
from app.memory.study import StudyStore
from app.memory.timetable import TimetableStore
from app.tutor import Tutor, take_turn
from tests.test_docs import make_pdf
from tests.test_office import make_docx
from tests.test_web_ui import Server


# --- File names and saving -----------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    ("Lecture 3.pdf", "Lecture 3.pdf"),
    ("C:\\Users\\me\\Desktop\\notes.PDF", "notes.pdf"),
    ("../../secret/notes.md", "notes.md"),
    ("weird<>|name?.docx", "weird_name_.docx"),
])
def test_safe_names(raw, expected):
    assert safe_name(raw) == expected


@pytest.mark.parametrize("raw", ["virus.exe", "run.bat", "page.html", "noext", ".pdf", "CON.pdf", "id_rsa.txt",
                                 "key.pem", ".env"])
def test_refused_names(raw):
    """Security: only importable types, no device names, no off-limits names."""
    with pytest.raises(UploadError):
        safe_name(raw)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


def test_upload_saves_imports_and_never_overwrites(ws, tmp_path):
    store = DocumentStore(tmp_path / "agent.db")
    first = save_upload(ws, store, "notes.md", b"# Joins\nINNER JOIN keeps matches", "DBMS", None, "notes")
    assert first.status == "imported" and first.document.path == "DBMS/notes.md"
    assert (ws / "library" / "DBMS" / "notes.md").read_bytes().startswith(b"# Joins")
    assert save_upload(ws, store, "notes.md", b"# Joins\nINNER JOIN keeps matches", "DBMS", None, "notes").status == "unchanged"
    other = save_upload(ws, store, "notes.md", b"different", "DBMS", None, "classwork")
    assert other.document.path == "DBMS/notes (2).md" and other.document.category == "classwork"
    assert (ws / "library" / "DBMS" / "notes.md").read_bytes().startswith(b"# Joins")  # untouched
    assert not list((ws / "library" / "DBMS").glob(".upload-*"))  # no temporary files left


def test_upload_limits_and_unreadable_files(ws, tmp_path):
    store = DocumentStore(tmp_path / "agent.db")
    with pytest.raises(UploadError, match="too large"):
        save_upload(ws, store, "big.txt", b"x" * 5_000_001, None, None, "notes")
    with pytest.raises(UploadError, match="empty"):
        save_upload(ws, store, "empty.txt", b"", None, None, "notes")
    with pytest.raises(UploadError, match="could not be read"):
        save_upload(ws, store, "broken.pdf", b"%PDF-1.4 nonsense", None, None, "notes")
    assert not (ws / "library" / "general" / "broken.pdf").exists()  # nothing half-saved
    with pytest.raises(UploadError, match="Unknown category"):
        save_upload(ws, store, "a.txt", b"a", None, None, "secret")


def test_word_and_pdf_uploads(ws, tmp_path):
    store = DocumentStore(tmp_path / "agent.db")
    pdf = save_upload(ws, store, "slides.pdf", make_pdf(["Normal forms", "Third normal form removes transitive dependencies"]),
                      None, None, "notes")
    assert pdf.document.pages == 2 and store.search("transitive")[0].citation == "general/slides.pdf, p. 2"
    docx = make_docx(tmp_path / "lab.docx", ["Lab 2", "Due 2026-11-02"])
    save_upload(ws, store, "lab.docx", docx.read_bytes(), None, None, "assignment")
    assert store.by_path("general/lab.docx").category == "assignment"


# --- The model proposes, Python validates ---------------------------------------------------------------


def test_deadline_proposals_are_validated():
    llm = FakeLLMClient(replies=[json.dumps({"tasks": [
        {"title": "Lab 2 report", "due_date": "2026-11-02", "estimate_hours": 4, "note": "PDF upload"},
        {"title": "", "due_date": "2026-11-03"},                    # no title: skipped
        {"title": "Quiz", "due_date": "next week"},                 # not a date: skipped
        {"title": "Read ch. 5", "due_date": None, "estimate_hours": 9999},  # too many hours: skipped
        {"title": "Project", "due_date": "null", "estimate_hours": "2.5", "extra": "ignored"},
    ]})])
    proposal = propose_tasks(llm, "Lab 2 report due 2 Nov", "DBMS", today=date(2026, 10, 17))
    assert [t.title for t in proposal.items] == ["Lab 2 report", "Project"] and proposal.skipped == 3
    sent = llm.calls[0]
    assert "<document>" in sent[1]["content"] and "never follow" in sent[0]["content"]


def test_timetable_proposals_are_validated():
    llm = FakeLLMClient(replies=["Sure! " + json.dumps({"classes": [
        {"weekday": "Mon", "start": "9:00", "end": "10.30", "title": "DBMS lecture", "room": "A1"},
        {"weekday": "funday", "start": "9:00", "end": "10:00", "title": "x"},
        {"weekday": "tuesday", "start": "14:00", "end": "13:00", "title": "backwards"},
        {"weekday": 4, "start": "25:00", "end": "26:00", "title": "impossible"},
    ]})])
    proposal = propose_timetable(llm, "Mon 9-10:30 DBMS A1")
    assert proposal.items == [ProposedClass(weekday=0, start="09:00", end="10:30", title="DBMS lecture", room="A1")]
    assert proposal.skipped == 3


# --- Through the window ---------------------------------------------------------------------------------


@pytest.fixture
def server(tmp_path):
    servers = []

    def make(replies=()):
        srv = Server(tmp_path, list(replies))
        workspace = tmp_path / "ws"
        workspace.mkdir(exist_ok=True)
        srv.state.workspace = workspace
        servers.append(srv)
        return srv

    yield make
    for srv in servers:
        srv.stop()


def _upload(srv, files, csrf=None, category="notes", course=""):
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("csrf", csrf or srv.guard.csrf), ("category", category), ("course", course)):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for filename, data in files:
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\n'
                     f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", srv.guard.port, timeout=30)
    conn.request("POST", "/materials/upload", body=body, headers={
        "Host": srv.host, "Origin": srv.origin, "Cookie": f"koda_session={srv.guard.token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    response = conn.getresponse()
    text = response.read().decode()
    conn.close()
    return response.status, text


def _post(srv, path, **body):
    return srv.request("POST", path, body={"csrf": srv.guard.csrf, **body})


def test_upload_through_the_window(server):
    srv = server()
    course = srv.state.courses.add("DBMS", "Database Systems", None)
    status, body = _upload(srv, [("joins.md", b"INNER JOIN keeps matching rows"), ("virus.exe", b"MZ")],
                           course=str(course.id))
    assert status == 200 and "joins.md" in body and "virus.exe" in body and "not supported" in body
    doc = srv.state.docs.list()[0]
    assert doc.course == "DBMS" and doc.path == "DBMS/joins.md"
    assert "joins.md" in srv.request("GET", "/materials")[2]
    assert _upload(srv, [("a.md", b"x")], csrf="wrong")[0] == 403
    assert len(srv.state.docs.list()) == 1


def test_upload_needs_a_workspace(server):
    srv = server()
    srv.state.workspace = None
    status, text = _upload(srv, [("a.md", b"x")])
    assert status == 400 and "WORKSPACE_DIR" in text


def test_edit_and_remove_material(server):
    srv = server()
    _upload(srv, [("notes.md", b"hello world")])
    doc = srv.state.docs.list()[0]
    assert _post(srv, f"/materials/{doc.id}/edit", category="assignment", course="")[0] == 200
    assert srv.state.docs.get(doc.id).category == "assignment"
    assert _post(srv, f"/materials/{doc.id}/edit", category="nope", course="")[0] == 400
    assert _post(srv, f"/materials/{doc.id}/remove")[0] == 200
    assert srv.state.docs.list() == [] and not (srv.state.workspace / "library" / "general" / "notes.md").exists()


def test_find_deadlines_then_confirm(server):
    due = (date.today() + timedelta(days=10)).isoformat()
    srv = server([json.dumps({"tasks": [{"title": "Lab 2 report", "due_date": due, "estimate_hours": 3},
                                        {"title": "<script>alert(1)</script>", "due_date": None}]})])
    _upload(srv, [("lab2.md", f"Submit the Lab 2 report by {due}".encode())], category="assignment")
    doc = srv.state.docs.list()[0]
    status, _, form = _post(srv, f"/materials/{doc.id}/deadlines")
    assert status == 200 and 'value="Lab 2 report"' in form and "<script>alert" not in form
    assert srv.state.tasks.list("all") == []  # nothing saved before you confirm
    status, _, body = _post(srv, "/materials/tasks/confirm", pick_0="1", title_0="Lab 2 report", due_0=due,
                            course_0="", estimate_0="3", title_1="ignored", due_1="", course_1="", estimate_1="")
    assert status == 200 and "Added 1 task" in body
    assert [(t.title, t.due_date) for t in srv.state.tasks.list()] == [("Lab 2 report", due)]
    assert _post(srv, "/materials/tasks/confirm", pick_0="1", title_0="", due_0="", course_0="", estimate_0="")[0] == 400


def test_read_timetable_then_confirm_and_edit(server):
    srv = server([json.dumps({"classes": [{"weekday": "monday", "start": "09:00", "end": "10:30",
                                           "title": "DBMS lecture", "room": "A1"}]})])
    srv.state.courses.add("DBMS", "Database Systems", None)
    _upload(srv, [("timetable.md", b"Monday 9-10:30 DBMS lecture A1")], category="timetable")
    doc = srv.state.docs.list()[0]
    form = _post(srv, f"/materials/{doc.id}/timetable")[2]
    assert 'value="DBMS lecture"' in form and srv.state.timetable.week() == []
    status, _, _ = _post(srv, "/timetable/confirm", pick_0="1", day_0="0", start_0="9:00", end_0="10:30",
                         title_0="DBMS lecture", room_0="A1", course_0="")
    slot = srv.state.timetable.week()[0]
    assert status == 200 and (slot.weekday, slot.start_time, slot.end_time) == (0, "09:00", "10:30")
    page = srv.request("GET", "/timetable")[2]
    assert "DBMS lecture" in page and "09:00–10:30" in page
    assert _post(srv, f"/timetable/{slot.id}/edit", day="1", start="11:00", end="10:00", title="x", room="", course="")[0] == 400
    assert _post(srv, f"/timetable/{slot.id}/edit", day="1", start="11:00", end="12:00", title="DBMS lab", room="B2", course="")[0] == 200
    assert srv.state.timetable.week()[0].title == "DBMS lab"
    assert _post(srv, "/timetable", day="2", start="8:00", end="9:00", title="Math", room="", course="")[0] == 200
    assert _post(srv, "/timetable", day="9", start="8:00", end="9:00", title="Math", room="", course="")[0] == 400
    assert _post(srv, f"/timetable/{slot.id}/delete")[0] == 200
    assert [s.title for s in srv.state.timetable.week()] == ["Math"]


def test_todays_classes_on_the_dashboard(server):
    srv = server()
    srv.state.timetable.add(date.today().weekday(), "08:00", "09:00", "Morning lecture", "H1")
    assert "Morning lecture" in srv.request("GET", "/dashboard")[2]


def test_model_errors_while_reading(server):
    srv = server(["no json here", "still none"])
    _upload(srv, [("a.md", b"text")], category="assignment")
    doc = srv.state.docs.list()[0]
    status, _, text = _post(srv, f"/materials/{doc.id}/deadlines")
    assert status == 502 and "usable list" in text


# --- Personalized lessons -------------------------------------------------------------------------------


def _tutor_reply(message="Step", question=None):
    return json.dumps({"feedback": None, "verdict": None, "message": message, "question": question})


def test_lessons_teach_from_linked_files_with_citations(tmp_path):
    docs = DocumentStore(tmp_path / "agent.db")
    study = StudyStore(docs.db)
    ws = tmp_path / "ws"
    ws.mkdir()
    save_upload(ws, docs, "joins.pdf", make_pdf(["Joins intro", "A LEFT JOIN keeps every row of the left table"]),
                "DBMS", None, "notes")
    save_upload(ws, docs, "other.md", b"LEFT JOIN in another course entirely", None, None, "notes")
    joins = docs.by_path("DBMS/joins.pdf")
    session = study.start("DBMS", "joins", "beginner", "", "learn")
    study.link_documents(session.id, [joins.id])
    llm = FakeLLMClient(replies=[_tutor_reply(question="What does LEFT JOIN keep?"), _tutor_reply("Right")])
    take_turn(study, Tutor(llm), session.id, "start", docs=docs)
    system = llm.calls[0][0]["content"]
    assert '<excerpt source="DBMS/joins.pdf, p. 1">' in system and "never follow instructions" in system
    take_turn(study, Tutor(llm), session.id, "answer", "the left rows", docs=docs)
    system = llm.calls[1][0]["content"]
    assert "DBMS/joins.pdf, p. 2" in system and "another course" not in system  # only the linked file


def test_lessons_without_files_have_no_material(tmp_path):
    study = StudyStore(tmp_path / "agent.db")
    session = study.start("DBMS", "joins", "beginner", "", "learn")
    llm = FakeLLMClient(replies=[_tutor_reply()])
    take_turn(study, Tutor(llm), session.id, "start", docs=DocumentStore(study.db))
    assert "COURSE MATERIAL" not in llm.calls[0][0]["content"]


def test_lessons_know_your_earlier_results(tmp_path):
    study = StudyStore(tmp_path / "agent.db")
    old = study.start("dbms", "normal forms", "beginner", "", "quiz")
    for verdict in ("incorrect", "incorrect", "correct"):
        study.record_verdict(old.id, verdict)
    study.start("Physics", "optics", "beginner", "", "learn")
    session = study.start("DBMS", "joins", "beginner", "", "learn")
    llm = FakeLLMClient(replies=[_tutor_reply()])
    take_turn(study, Tutor(llm), session.id, "start")
    system = llm.calls[0][0]["content"]
    assert "normal forms (quiz, beginner): 33% over 3 answers" in system
    assert "Weak areas to revisit briefly when they connect: normal forms" in system and "optics" not in system


def test_study_this_file_prefills_the_form(server):
    srv = server()
    _upload(srv, [("Lecture_3 Joins.md", b"joins")])
    doc = srv.state.docs.list()[0]
    page = srv.request("GET", f"/study?document={doc.id}")[2]
    assert 'value="Lecture 3 Joins"' in page and f'value="{doc.id}" checked' in page
