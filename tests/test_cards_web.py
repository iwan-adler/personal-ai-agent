"""Flashcard practice in the window: the same rules as /quiz, over real HTTP."""

import re

import pytest

from app.memory.cards import NewCard
from tests.test_web_ui import Server


@pytest.fixture
def server(tmp_path):
    srv = Server(tmp_path, [])
    yield srv
    srv.stop()


def _post(srv, path, **body):
    return srv.request("POST", path, body={"csrf": srv.guard.csrf, **body})


def _round_state(html):
    """The hidden fields the next click sends back, like the browser would."""
    fields = dict(re.findall(r'<input type="hidden" name="(course|again|repeated|tally)" value="([^"]*)"', html))
    card = re.search(r'hx-post="/cards/(\d+)/grade"', html)
    return (int(card.group(1)) if card else None), fields


def _grade(srv, html, grade):
    card_id, fields = _round_state(html)
    return _post(srv, f"/cards/{card_id}/grade", grade=str(grade), **fields)[2]


def test_a_practice_round_like_quiz(server):
    server.state.cards.add_many([NewCard("Q1", "A1"), NewCard("Q2", "A2"), NewCard("Q3", "A3")])
    html = _post(server, "/cards/practice", course="")[2]
    assert "Q1" in html and "3 left in this round" in html and ">Again<small>today</small>" in html
    html = _grade(server, html, 3)   # Q1 good
    assert "Q2" in html
    html = _grade(server, html, 1)   # Q2 again: set aside, not shown right away
    assert "Q3" in html
    html = _grade(server, html, 3)   # Q3 good
    assert "Q2" in html and "1 left in this round" in html  # Q2 once more at the end
    html = _grade(server, html, 1)   # again again: not repeated a third time
    assert "Round finished" in html and "4" in html and "2 again, 2 good" in html
    with server.state.cards.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 4  # every click was saved


def test_practice_by_course(server):
    dbms = server.state.courses.add("DBMS", "Database Systems", None)
    server.state.cards.add_many([NewCard("joins?", "rows")], course_id=dbms.id)
    server.state.cards.add_many([NewCard("other", "x")])
    page = server.request("GET", "/cards")[2]
    assert f'value="{dbms.id}"' in page and "DBMS (1)" in page and "All (2)" in page
    html = _post(server, "/cards/practice", course=str(dbms.id))[2]
    assert "joins?" in html and "other" not in html
    assert "Round finished" in _grade(server, html, 4)
    assert _post(server, "/cards/practice", course="999")[0] == 400


def test_practice_needs_csrf_and_valid_values(server):
    """Security: another site cannot grade your cards; tampered round data is refused."""
    server.state.cards.add_many([NewCard("Q", "A")])
    card_id = server.state.cards.list()[0].id
    good = {"course": "", "again": "", "repeated": "", "tally": "0,0,0,0"}
    assert server.request("POST", "/cards/practice", body={"course": "", "csrf": "x"})[0] == 403
    assert server.request("POST", f"/cards/{card_id}/grade", body={"grade": "3", "csrf": "x", **good})[0] == 403
    assert _post(server, f"/cards/{card_id}/grade", grade="5", **good)[0] == 400
    assert _post(server, f"/cards/{card_id}/grade", grade="3", **{**good, "again": "1;DROP"})[0] == 400
    assert _post(server, f"/cards/{card_id}/grade", grade="3", **{**good, "tally": "1,2"})[0] == 400
    assert _post(server, "/cards/9999/grade", grade="3", **good)[0] == 404
    with server.state.cards.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_card_text_is_escaped(server):
    server.state.cards.add_many([NewCard("<script>alert(1)</script> **bold**", "<img src=x onerror=alert(1)>")])
    html = _post(server, "/cards/practice", course="")[2]
    assert "<script>alert" not in html and "<img src=x" not in html and "<strong>bold</strong>" in html


def test_empty_states(server):
    assert "no flashcards yet" in server.request("GET", "/cards")[2]
    server.state.cards.add_many([NewCard("Q", "A")])
    html = _post(server, "/cards/practice", course="")[2]
    _grade(server, html, 4)
    page = server.request("GET", "/cards")[2]
    assert "All caught up" in page and "Practice now" not in server.request("GET", "/dashboard")[2]


def test_counters_update_after_each_grade(server):
    server.state.cards.add_many([NewCard("Q1", "A1"), NewCard("Q2", "A2")])
    html = _grade(server, _post(server, "/cards/practice", course="")[2], 3)
    stats = html.split('id="card-stats" hx-swap-oob="true"', 1)[1]
    assert '<span class="num">1</span><span class="lbl">due today</span>' in stats
    assert '<span class="num">1</span><span class="lbl">reviewed today</span>' in stats
