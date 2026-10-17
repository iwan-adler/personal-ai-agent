"""Course tools, plus the optional `course` parameter other tools share.

Risk choices:
- course_list only reads -> SAFE, courses.read. Course data is the user's own
  (confirmed when added), so it is not outside content.
- course_add / course_update change data that outlives the session ->
  CONFIRMATION, courses.write. course_update's prompt shows old -> new.
- No delete tool: only the user's /forget-course command deletes, and it only
  clears links; tasks, facts and documents stay.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.memory.courses import CODE_PATTERN, CourseStore
from app.tools.base import RiskLevel, Tool, ToolContext, ToolRegistry

COURSE_FIELD_DESCRIPTION = "Optional course code (see course_list), e.g. 'DBMS'."


def course_field():
    """The optional `course` parameter of task, fact and document tools."""
    return Field(default=None, pattern=CODE_PATTERN, description=COURSE_FIELD_DESCRIPTION)


def course_store(ctx: ToolContext) -> CourseStore:
    if ctx.course_store is None:
        raise RuntimeError("Courses are not configured for this session.")
    return ctx.course_store


def course_id_for(ctx: ToolContext, code: str | None) -> int | None:
    """None for no course; raises CourseNotFound ("Unknown course 'X'; see course_list.")."""
    if code is None:
        return None
    return course_store(ctx).get(code).id


class CourseAddInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(pattern=CODE_PATTERN, description="Short unique code, e.g. 'DBMS' or 'OS1'.")
    name: str = Field(min_length=1, max_length=100)
    semester: str | None = Field(default=None, max_length=20, description="Free text, e.g. 'WS 2026/27'.")


class CourseUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    code: str = Field(pattern=CODE_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    semester: str | None = Field(default=None, max_length=20)


class CourseListInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CourseOut(BaseModel):
    code: str
    name: str
    semester: str | None


class CourseSummary(CourseOut):
    open_tasks: int
    documents: int
    facts: int
    next_due_date: str | None
    next_due_title: str | None


class CourseListOutput(BaseModel):
    count: int
    courses: list[CourseSummary]


class CourseOutput(BaseModel):
    course: CourseOut


def _out(course) -> CourseOut:
    return CourseOut(code=course.code, name=course.name, semester=course.semester)


def _course_list(ctx: ToolContext) -> CourseListOutput:
    courses = [
        CourseSummary(
            code=s.course.code, name=s.course.name, semester=s.course.semester,
            open_tasks=s.open_tasks, documents=s.documents, facts=s.facts,
            next_due_date=s.next_due_date, next_due_title=s.next_due_title,
        )
        for s in course_store(ctx).list()
    ]
    return CourseListOutput(count=len(courses), courses=courses)


def _preview_update(ctx: ToolContext, args: CourseUpdateInput) -> str:
    current = course_store(ctx).get(args.code)
    lines = [f"Change course {current.code}:"]
    if args.name is not None and args.name != current.name:
        lines.append(f"  name:     {current.name!r} -> {args.name!r}")
    if args.semester is not None and args.semester != current.semester:
        lines.append(f"  semester: {current.semester!r} -> {args.semester!r}")
    if len(lines) == 1:
        lines.append("  no changes")
    return "\n".join(lines)


def register_course_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="course_list",
            description=(
                "List the user's courses with open tasks, documents, facts and the next deadline. "
                "Exams are tasks linked to a course."
            ),
            input_model=CourseListInput,
            output_model=CourseListOutput,
            permission="courses.read",
            risk=RiskLevel.SAFE,
            handler=lambda ctx, args: _course_list(ctx),
        )
    )
    registry.register(
        Tool(
            name="course_add",
            description="Add a course. Link tasks (including exams), facts and documents to it via their course parameter.",
            input_model=CourseAddInput,
            output_model=CourseOutput,
            permission="courses.write",
            risk=RiskLevel.CONFIRMATION,
            handler=lambda ctx, args: CourseOutput(
                course=_out(course_store(ctx).add(args.code, args.name, args.semester))),
        )
    )
    registry.register(
        Tool(
            name="course_update",
            description="Change a course's name or semester.",
            input_model=CourseUpdateInput,
            output_model=CourseOutput,
            permission="courses.write",
            risk=RiskLevel.CONFIRMATION,
            handler=lambda ctx, args: CourseOutput(
                course=_out(course_store(ctx).update(args.code, args.name, args.semester))),
            preview=_preview_update,
        )
    )
