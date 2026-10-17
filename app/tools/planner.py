"""plan_week: the deterministic planner (app/planner.py) as a tool.

Risk: only reads the user's own tasks and flashcard counts -> SAFE, tasks.read,
not outside content. The plan is computed in Python; the model only explains it.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from app.memory.cards import CardStore
from app.memory.tasks import TaskStore
from app.planner import Plan, PlanTask, build_plan, format_plan
from app.tools.base import RiskLevel, Tool, ToolContext, ToolRegistry

MAX_TASKS = 1000


def compute_plan(tasks: TaskStore, cards: CardStore | None, study_hours: tuple[float, ...] | None,
                 days: int = 7, today: date | None = None) -> Plan:
    today = today or date.today()
    open_tasks = [
        PlanTask(
            id=t.id,
            title=t.title,
            due_date=date.fromisoformat(t.due_date) if t.due_date else None,
            estimate_hours=t.estimate_hours,
            course=t.course,
        )
        for t in tasks.list(status="open", limit=MAX_TASKS)
    ]
    cards_due = cards.due_counts(today, days) if cards is not None else {}
    return build_plan(open_tasks, study_hours, today, days, cards_due)


class PlanWeekInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: int = Field(default=7, ge=1, le=28)


class PlanWeekOutput(BaseModel):
    plan: str
    warnings: int
    assumed_default_hours: bool


def register_planner_tools(registry: ToolRegistry, study_hours: tuple[float, ...] | None) -> None:
    def handler(ctx: ToolContext, args: PlanWeekInput) -> PlanWeekOutput:
        if ctx.task_store is None:
            raise RuntimeError("Task storage is not configured for this session.")
        hours = ctx.study_hours() if ctx.study_hours is not None else study_hours
        plan = compute_plan(ctx.task_store, ctx.card_store, hours, args.days)
        return PlanWeekOutput(plan=format_plan(plan), warnings=len(plan.warnings),
                              assumed_default_hours=plan.assumed_default_hours)

    registry.register(
        Tool(
            name="plan_week",
            description=(
                "The user's study plan for the next days, computed deterministically from open "
                "tasks (deadline + estimate_hours), study hours and due flashcards. Present it "
                "and explain warnings; do not invent a different schedule."
            ),
            input_model=PlanWeekInput,
            output_model=PlanWeekOutput,
            permission="tasks.read",
            risk=RiskLevel.SAFE,
            handler=handler,
            max_output_bytes=16_384,
        )
    )
