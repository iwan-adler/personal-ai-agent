"""Flashcard tools for the model: propose a batch, or read counts.

Risk choices:
- card_propose saves study material that outlives the session -> CONFIRMATION,
  cards.write. The prompt shows every card in full, with its source.
- card_stats -> SAFE, cards.read, and returns counts only. Card text never goes
  back to the model: it usually comes from documents (outside content), and
  studying happens in the CLI (/quiz) without the model.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.memory.cards import CardStore, NewCard
from app.tools.base import RiskLevel, Tool, ToolContext, ToolRegistry
from app.tools.courses import course_field, course_id_for

MAX_CARDS_PER_BATCH = 20


def _store(ctx: ToolContext) -> CardStore:
    if ctx.card_store is None:
        raise RuntimeError("Flashcards are not configured for this session.")
    return ctx.card_store


class CardIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    front: str = Field(min_length=1, max_length=300, description="The question.")
    back: str = Field(min_length=1, max_length=1000, description="The answer.")
    source: str | None = Field(default=None, max_length=200,
                               description="The citation from doc_search, e.g. 'dbms/lecture3.pdf, p. 2'.")


class CardProposeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cards: list[CardIn] = Field(min_length=1, max_length=MAX_CARDS_PER_BATCH)
    course: str | None = course_field()


class CardProposeOutput(BaseModel):
    created: int
    skipped_duplicates: int
    course: str | None


class CardStatsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    course: str | None = course_field()


class CourseCardCount(BaseModel):
    course: str | None
    total: int
    due: int


class CardStatsOutput(BaseModel):
    total: int
    due_today: int
    reviewed_today: int
    by_course: list[CourseCardCount]


def _propose(ctx: ToolContext, args: CardProposeInput) -> CardProposeOutput:
    course_id = course_id_for(ctx, args.course)
    created, skipped = _store(ctx).add_many(
        [NewCard(front=c.front, back=c.back, source=c.source) for c in args.cards], course_id
    )
    return CardProposeOutput(created=len(created), skipped_duplicates=skipped, course=args.course)


def _preview(ctx: ToolContext, args: CardProposeInput) -> str:
    where = f" for course {args.course}" if args.course else ""
    lines = [f"Save {len(args.cards)} flashcard(s){where}:"]
    for number, card in enumerate(args.cards, start=1):
        lines.append(f"{number:>3}. Q: {card.front}")
        lines.append(f"     A: {card.back}")
        lines.append(f"     Source: {card.source or '(none given)'}")
    return "\n".join(lines)


def _stats(ctx: ToolContext, args: CardStatsInput) -> CardStatsOutput:
    stats = _store(ctx).stats(course_id_for(ctx, args.course))
    return CardStatsOutput(
        total=stats.total,
        due_today=stats.due,
        reviewed_today=stats.reviewed_today,
        by_course=[CourseCardCount(course=c, total=t, due=d) for c, t, d in stats.by_course],
    )


def register_card_tools(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="card_propose",
            description=(
                "Propose up to 20 flashcards (question, answer, source citation) for the user to "
                "approve, ideally from doc_search hits. The user studies them with /quiz."
            ),
            input_model=CardProposeInput,
            output_model=CardProposeOutput,
            permission="cards.write",
            risk=RiskLevel.CONFIRMATION,
            handler=_propose,
            preview=_preview,
        )
    )
    registry.register(
        Tool(
            name="card_stats",
            description="Counts of the user's flashcards: total, due today, reviewed today, per course.",
            input_model=CardStatsInput,
            output_model=CardStatsOutput,
            permission="cards.read",
            risk=RiskLevel.SAFE,
            handler=_stats,
        )
    )
