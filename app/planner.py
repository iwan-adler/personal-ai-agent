"""Deterministic study planner (V0.7). Pure: no database, no clock, no model.

The model only explains a plan; it never computes one, so the same tasks and
hours always give the same plan, and the plan is testable with fixed dates.

Rules:
1. Each day's capacity is the study hours for its weekday, minus the time for
   that day's due flashcards (SECONDS_PER_CARD each).
2. Open tasks with a deadline and an estimate are scheduled earliest deadline
   first, day by day from today, in quarter hours.
3. A task can be worked on until the day *before* its deadline.
4. Warnings: not enough time before a deadline (with the shortfall), overdue,
   and due today with work left. Tasks without an estimate or without a
   deadline are listed, not scheduled.

All arithmetic is in whole quarter hours, so there is no float drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

SECONDS_PER_CARD = 30
DEFAULT_HOURS_PER_DAY = 2.0
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class PlanTask:
    id: int
    title: str
    due_date: date | None
    estimate_hours: float | None
    course: str | None = None


@dataclass(frozen=True)
class Block:
    task_id: int
    title: str
    course: str | None
    hours: float


@dataclass
class Day:
    date: date
    capacity_hours: float
    card_minutes: float
    blocks: list[Block] = field(default_factory=list)

    @property
    def planned_hours(self) -> float:
        return sum(block.hours for block in self.blocks)


@dataclass
class Plan:
    days: list[Day]
    warnings: list[str]
    unestimated: list[PlanTask]
    no_deadline: list[PlanTask]
    assumed_default_hours: bool


def _quarters(hours: float) -> int:
    return int(round(hours * 4))


def _hours(quarters: int) -> float:
    return quarters / 4


def _fmt(hours: float) -> str:
    return f"{hours:g} h"


def build_plan(
    tasks: list[PlanTask],
    hours_by_weekday: tuple[float, ...] | None,
    today: date,
    days: int = 7,
    cards_due: dict[date, int] | None = None,
) -> Plan:
    """hours_by_weekday: 7 values, Monday first; None means DEFAULT_HOURS_PER_DAY.
    cards_due: number of flashcards due on each day (today's includes overdue)."""
    assumed = hours_by_weekday is None
    hours = hours_by_weekday or (DEFAULT_HOURS_PER_DAY,) * 7
    cards_due = cards_due or {}

    plan_days: list[Day] = []
    free: list[int] = []
    for offset in range(days):
        day = today + timedelta(days=offset)
        card_minutes = cards_due.get(day, 0) * SECONDS_PER_CARD / 60
        capacity_q = max(0, _quarters(hours[day.weekday()]) - math.ceil(card_minutes / 15))
        plan_days.append(Day(date=day, capacity_hours=_hours(capacity_q), card_minutes=card_minutes))
        free.append(capacity_q)

    warnings: list[str] = []
    unestimated = [t for t in tasks if t.estimate_hours is None and t.due_date is not None]
    no_deadline = [t for t in tasks if t.due_date is None]
    schedulable = sorted(
        (t for t in tasks if t.due_date is not None and t.estimate_hours is not None),
        key=lambda t: (t.due_date, t.id),
    )
    horizon_end = today + timedelta(days=days - 1)

    for task in schedulable:
        assert task.due_date is not None and task.estimate_hours is not None
        needed = math.ceil(task.estimate_hours * 4)  # round effort up, never down
        remaining = needed
        if task.due_date < today:
            warnings.append(f"Overdue: {task.title!r} was due {task.due_date} ({_fmt(_hours(remaining))} left).")
            continue
        if task.due_date == today:
            warnings.append(f"Due today: {task.title!r} still needs {_fmt(_hours(remaining))}.")
            continue
        last_day = task.due_date - timedelta(days=1)
        for index, day in enumerate(plan_days):
            if remaining == 0 or day.date > last_day:
                break
            take = min(remaining, free[index])
            if take:
                day.blocks.append(Block(task.id, task.title, task.course, _hours(take)))
                free[index] -= take
                remaining -= take
        if remaining and last_day <= horizon_end:
            warnings.append(
                f"Not enough time: {task.title!r} needs {_fmt(_hours(needed))}, but only "
                f"{_fmt(_hours(needed - remaining))} is available before {task.due_date} "
                f"({_fmt(_hours(remaining))} short)."
            )
        elif remaining:
            warnings.append(
                f"Continues later: {task.title!r} (due {task.due_date}) still needs "
                f"{_fmt(_hours(remaining))} after this plan period."
            )

    return Plan(days=plan_days, warnings=warnings, unestimated=unestimated,
                no_deadline=no_deadline, assumed_default_hours=assumed)


def parse_study_hours(text: str) -> tuple[float, ...]:
    """'mon=2,tue=2.5,sat=4' -> 7 values, Monday first; missing days are 0."""
    values = dict.fromkeys(WEEKDAYS, 0.0)
    seen: set[str] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        key, sep, raw = part.partition("=")
        key = key.strip().lower()
        if not sep or key not in values:
            raise ValueError("STUDY_HOURS must look like mon=2,tue=2,...,sun=0")
        if key in seen:
            raise ValueError(f"STUDY_HOURS lists {key} twice")
        seen.add(key)
        try:
            number = float(raw)
        except ValueError:
            raise ValueError(f"STUDY_HOURS: {key} needs a number of hours") from None
        if not 0 <= number <= 16 or math.isnan(number):
            raise ValueError("STUDY_HOURS: each day must be 0-16 hours")
        values[key] = number
    if not seen:
        raise ValueError("STUDY_HOURS must look like mon=2,tue=2,...,sun=0")
    return tuple(values[day] for day in WEEKDAYS)


def format_plan(plan: Plan) -> str:
    """Plain text for the CLI and for the model to explain."""
    lines: list[str] = []
    if plan.assumed_default_hours:
        lines.append(f"(No STUDY_HOURS set: assuming {_fmt(DEFAULT_HOURS_PER_DAY)} per day.)")
    for day in plan.days:
        cards = f", flashcards {day.card_minutes:g} min" if day.card_minutes else ""
        lines.append(f"{WEEKDAYS[day.date.weekday()].title()} {day.date}: "
                     f"{_fmt(day.planned_hours)} of {_fmt(day.capacity_hours)}{cards}")
        for block in day.blocks:
            course = f"[{block.course}] " if block.course else ""
            lines.append(f"    {_fmt(block.hours):>6}  {course}{block.title} (#{block.task_id})")
    for warning in plan.warnings:
        lines.append(f"! {warning}")
    if plan.unestimated:
        lines.append("Needs an estimate: " + ", ".join(f"{t.title} (#{t.id})" for t in plan.unestimated))
    if plan.no_deadline:
        lines.append("No deadline (not scheduled): " + ", ".join(f"{t.title} (#{t.id})" for t in plan.no_deadline))
    return "\n".join(lines)
