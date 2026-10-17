"""Flashcard practice in the window: the web version of /quiz (no model involved).

Each grade is saved the moment you click it, with the same SM-2 rules as the
CLI. The round's state (tally, cards answered "again") travels in the form, so
the server keeps nothing between clicks. "Again" makes a card due today again,
so, as in /quiz, such a card is set aside and shown once more at the end of
the round instead of right away (otherwise it would come back forever).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.main import ChatState
from app.memory.cards import GRADE_NAMES, CardNotFound, next_schedule

MAX_ROUND_IDS = 500
_IDS = re.compile(r"^\d{1,12}(,\d{1,12})*$")


def _ids(text: str) -> list[int]:
    text = text.strip()
    if not text:
        return []
    if not _IDS.match(text):
        raise ValueError("bad id list")
    ids = [int(part) for part in text.split(",")]
    if len(ids) > MAX_ROUND_IDS:
        raise ValueError("too many ids")
    return ids


def _join(ids: list[int]) -> str:
    return ",".join(str(i) for i in ids)


def _intervals(card, today: date) -> dict[int, str]:
    """What each grade would do, shown on its button (like Anki)."""
    out = {}
    for grade in GRADE_NAMES:
        days = next_schedule(card.schedule, grade, today).interval_days
        out[grade] = "today" if days == 0 else f"{days} d" if days < 31 else f"{round(days / 30)} mo"
    return out


def add_card_routes(app: FastAPI, *, state: ChatState, render: Callable[..., HTMLResponse],
                    csrf_ok: Callable[[str], bool], plain: Callable[[int, str], Response]) -> None:
    cards = state.cards

    def course_choices() -> list[dict]:
        stats = cards.stats()
        due_by_code = {code: (total, due) for code, total, due in stats.by_course}
        courses = state.courses.list() if state.courses else []
        return [{"id": s.course.id, "code": s.course.code, "name": s.course.name,
                 "total": due_by_code.get(s.course.code, (0, 0))[0], "due": due_by_code.get(s.course.code, (0, 0))[1]}
                for s in courses]

    def next_card(course_id: int | None, again: list[int], repeated: list[int]):
        """The next due card not set aside; then the set-aside ones, each once."""
        skip = set(again) | set(repeated)
        for card in cards.due(course_id, limit=len(skip) + 1):
            if card.id not in skip:
                return card, again, repeated
        while again:
            card_id, again = again[0], again[1:]
            try:
                card = cards.get(card_id)
            except CardNotFound:
                continue
            if course_id is None or card.course_id == course_id:
                return card, again, repeated + [card_id]
        return None, again, repeated

    def practice(request: Request, course_id: int | None, again: list[int], repeated: list[int],
                 tally: dict[str, int], message: str | None = None) -> HTMLResponse:
        card, again, repeated = next_card(course_id, again, repeated)
        today = date.today()
        return render(request, "_card_practice.html", card=card, course_id=course_id or "",
                      again=_join(again), repeated=_join(repeated), tally=tally,
                      reviewed=sum(tally.values()), intervals=_intervals(card, today) if card else {},
                      next_due=cards.next_due_date(course_id), message=message, stats=cards.stats(),
                      remaining=remaining(course_id, card, again, repeated))

    def remaining(course_id, card, again, repeated) -> int:
        """Cards left in this round, the one on screen included."""
        if card is None:
            return 0
        skip = set(again) | set(repeated)
        fresh = sum(1 for c in cards.due(course_id, limit=MAX_ROUND_IDS) if c.id not in skip)
        return fresh + len(again) + (1 if card.id in repeated else 0)

    def parse_course(course: str) -> int | None:
        if not course.strip():
            return None
        course_id = int(course)
        if course_id not in {c["id"] for c in course_choices()}:
            raise ValueError("unknown course")
        return course_id

    @app.get("/cards", response_class=HTMLResponse)
    def cards_home(request: Request):
        return render(request, "cards.html", page="cards", stats=cards.stats(), courses=course_choices(),
                      next_due=cards.next_due_date())

    @app.post("/cards/practice", response_class=HTMLResponse)
    def cards_start(request: Request, course: str = Form(""), csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        try:
            course_id = parse_course(course)
        except ValueError:
            return plain(400, "Unknown course.")
        return practice(request, course_id, [], [], {name: 0 for name in GRADE_NAMES.values()})

    @app.post("/cards/{card_id}/grade", response_class=HTMLResponse)
    def cards_grade(request: Request, card_id: int, grade: int = Form(0), course: str = Form(""),
                    again: str = Form(""), repeated: str = Form(""), tally: str = Form("0,0,0,0"),
                    csrf: str = Form("")):
        if not csrf_ok(csrf):
            return plain(403, "Missing or wrong CSRF value.")
        if grade not in GRADE_NAMES:
            return plain(400, "Grade must be 1 (again), 2 (hard), 3 (good) or 4 (easy).")
        try:
            course_id = parse_course(course)
            again_ids, repeated_ids = _ids(again), _ids(repeated)
            counts = _ids(tally)
            if len(counts) != 4:
                raise ValueError("bad tally")
        except ValueError:
            return plain(400, "This practice round is out of date. Start a new one.")
        try:
            cards.review(card_id, grade)
        except CardNotFound:
            return plain(404, "That card no longer exists.")
        counts[grade - 1] += 1
        if grade == 1 and card_id not in again_ids and card_id not in repeated_ids:
            again_ids.append(card_id)  # once more at the end of this round
        names = list(GRADE_NAMES.values())
        return practice(request, course_id, again_ids, repeated_ids, dict(zip(names, counts)))
