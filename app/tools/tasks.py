"""Task tools: the model proposes, the safety layer reviews, these handlers act.

Risk choices:
- task_list only reads -> SAFE.
- task_add / task_complete change data that outlives the session -> CONFIRMATION,
  so they ask first even at autonomy level 3. Nothing here deletes data.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.memory.tasks import Task, TaskStore
from app.tools.base import RiskLevel, Tool, ToolContext, ToolRegistry
from app.tools.courses import course_field, course_id_for


class TaskOut(BaseModel):
    id: int
    title: str
    due_date: str | None
    status: str
    created_at: str
    completed_at: str | None
    course: str | None = None
    estimate_hours: float | None = None

    @classmethod
    def of(cls, task: Task) -> TaskOut:
        return cls(**task.__dict__)


class TaskAddInput(BaseModel):
    # Unknown fields are rejected so a confused model can't smuggle extra args.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=200)
    due_date: date | None = Field(
        default=None,
        description="Deadline as YYYY-MM-DD. Call time_now first to resolve words like 'Friday'.",
    )
    course: str | None = course_field()
    estimate_hours: float | None = Field(
        default=None, gt=0, le=200, description="Remaining effort in hours, for plan_week."
    )

    @field_validator("due_date")
    @classmethod
    def _not_in_past(cls, value: date | None) -> date | None:
        if value is not None and value < date.today():
            raise ValueError("due_date is in the past")
        return value


class TaskAddOutput(BaseModel):
    task: TaskOut


class TaskListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "done", "all"] = "open"
    limit: int = Field(default=20, ge=1, le=50)
    course: str | None = course_field()


class TaskListOutput(BaseModel):
    status: str
    count: int
    tasks: list[TaskOut]


class TaskCompleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(ge=1)


class TaskCompleteOutput(BaseModel):
    task: TaskOut


class TaskEstimateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(ge=1)
    hours: float = Field(gt=0, le=200, description="Remaining effort in hours.")


class TaskEstimateOutput(BaseModel):
    task: TaskOut


def _store(ctx: ToolContext) -> TaskStore:
    if ctx.task_store is None:
        raise RuntimeError("Task storage is not configured for this session.")
    return ctx.task_store


def task_add_handler(ctx: ToolContext, args: TaskAddInput) -> TaskAddOutput:
    task = _store(ctx).add(args.title, args.due_date, course_id_for(ctx, args.course), args.estimate_hours)
    return TaskAddOutput(task=TaskOut.of(task))


def task_list_handler(ctx: ToolContext, args: TaskListInput) -> TaskListOutput:
    tasks = _store(ctx).list(args.status, args.limit, course_id_for(ctx, args.course))
    return TaskListOutput(status=args.status, count=len(tasks), tasks=[TaskOut.of(t) for t in tasks])


def task_complete_handler(ctx: ToolContext, args: TaskCompleteInput) -> TaskCompleteOutput:
    return TaskCompleteOutput(task=TaskOut.of(_store(ctx).complete(args.task_id)))


def task_estimate_handler(ctx: ToolContext, args: TaskEstimateInput) -> TaskEstimateOutput:
    _, after = _store(ctx).set_estimate(args.task_id, args.hours)
    return TaskEstimateOutput(task=TaskOut.of(after))


def _preview_estimate(ctx: ToolContext, args: TaskEstimateInput) -> str:
    task = _store(ctx).get(args.task_id)
    before = f"{task.estimate_hours:g} h" if task.estimate_hours is not None else "no estimate"
    return f"Change the remaining effort of {task.title!r} (#{task.id}): {before} -> {args.hours:g} h"


def register_task_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="task_add",
            description="Add a task or deadline to the user's persistent task list.",
            input_model=TaskAddInput,
            output_model=TaskAddOutput,
            permission="tasks.write",
            risk=RiskLevel.CONFIRMATION,
            handler=task_add_handler,
        )
    )
    registry.register(
        Tool(
            name="task_list",
            description="List the user's tasks, earliest deadline first.",
            input_model=TaskListInput,
            output_model=TaskListOutput,
            permission="tasks.read",
            risk=RiskLevel.SAFE,
            handler=task_list_handler,
        )
    )
    registry.register(
        Tool(
            name="task_complete",
            description="Mark a task as done by its id (get ids from task_list).",
            input_model=TaskCompleteInput,
            output_model=TaskCompleteOutput,
            permission="tasks.write",
            risk=RiskLevel.CONFIRMATION,
            handler=task_complete_handler,
        )
    )
    registry.register(
        Tool(
            name="task_estimate",
            description="Set how many hours of work a task still needs (used by plan_week).",
            input_model=TaskEstimateInput,
            output_model=TaskEstimateOutput,
            permission="tasks.write",
            risk=RiskLevel.CONFIRMATION,
            handler=task_estimate_handler,
            preview=_preview_estimate,
        )
    )
