"""Study Mode tutor: teaches in small steps and asks one question at a time.

The tutor is a separate, much smaller loop than the agent: it has no tools, so
whatever the model says can only be shown to you, never acted on. The model
replies with JSON (message, optional feedback, a verdict for your last answer
and at most one question); Python decides what counts:

- a verdict is counted only for an "answer" action while a question was open;
- "I don't know" counts as incorrect, whatever the model says;
- texts are cut to fixed lengths, and malformed output gets one repair try,
  then the raw text is shown as a plain message (nothing is invented).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.llm.client import Completer
from app.memory.study import StudyMessage, StudySession, StudyStore

Action = Literal["start", "answer", "ask", "continue", "hint", "skip", "mode"]
ACTIONS: tuple[str, ...] = ("start", "answer", "ask", "continue", "hint", "skip", "mode")
MAX_STUDENT_CHARS = 4_000
MAX_MESSAGE_CHARS = 6_000
MAX_QUESTION_CHARS = 1_000
HISTORY_MESSAGES = 24

RULES = """You are Koda's study tutor for one university student. Teach in small steps:
at most about 150 words per reply. Never dump a whole chapter at once. Ask at most ONE
question per reply and then wait for the student's answer. Be encouraging but honest:
when an answer is wrong or incomplete, say so kindly, explain the right idea briefly and
move on. Use Markdown (short paragraphs, lists, `code`, tables when useful). If you are
unsure of a fact, say so instead of guessing.

Reply with ONE JSON object only, no text around it:
{"feedback": "feedback on the student's last answer, or null",
 "verdict": "correct" | "partial" | "incorrect" | null,
 "message": "what you teach or say now (Markdown)",
 "question": "the one question the student should answer next, or null"}
Give a verdict only when the student just answered your question."""

MODE_RULES = {
    "learn": "Mode LEARN: introduce the topic step by step, from basics to the goal. After each "
             "short step, check understanding with one simple question.",
    "revise": "Mode REVISE: the student has learned this before. Recap the key points briefly "
              "(a few bullets per step) and ask recall questions about them.",
    "practice": "Mode PRACTICE: give one exercise or problem at a time, matching the difficulty. "
                "On a wrong answer give a hint or the worked solution, then the next exercise.",
    "quiz": "Mode QUIZ: ask one question at a time without teaching first. After each answer give "
            "short feedback (one or two sentences) and the next question.",
}

ACTION_TEXT = {
    "start": "Start the session.",
    "continue": "Continue with the next step.",
    "hint": "Give me a hint for the open question. Do not reveal the answer.",
    "skip": "I don't know. Show me the answer, explain it briefly, then continue.",
}


class TutorReply(BaseModel):
    model_config = ConfigDict(extra="ignore")

    feedback: str | None = None
    verdict: Literal["correct", "partial", "incorrect"] | None = None
    message: str = ""
    question: str | None = None


@dataclass(frozen=True)
class TutorTurn:
    student_text: str | None  # what to show as your message (None for "start")
    tutor_text: str           # Markdown to show and store
    question: str | None      # the question now open, if any
    verdict: str | None       # what Python counted for your answer, if anything


def _system_prompt(session: StudySession) -> str:
    goal = f"\nLearning goal: {session.goal}" if session.goal else ""
    return (f"{RULES}\n\n{MODE_RULES[session.mode]}\n\nSubject: {session.subject}\nTopic: {session.topic}\n"
            f"Difficulty: {session.difficulty}{goal}")


def _parse(raw: str) -> TutorReply:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("not an object")
    reply = TutorReply.model_validate(data)
    if not (reply.message or reply.question or reply.feedback):
        raise ValueError("empty reply")
    return reply


def _clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def compose(reply: TutorReply) -> str:
    """The stored and shown Markdown: feedback, then the step, then the question."""
    parts = [p for p in (_clip(reply.feedback, MAX_MESSAGE_CHARS), _clip(reply.message, MAX_MESSAGE_CHARS)) if p]
    return "\n\n".join(parts)


class Tutor:
    def __init__(self, llm: Completer) -> None:
        self.llm = llm

    def respond(self, session: StudySession, history: list[StudyMessage], action: str,
                text: str = "", new_mode: str | None = None) -> TutorTurn:
        if action not in ACTIONS:
            raise ValueError("Unknown action.")
        text = text.strip()
        if action in ("answer", "ask") and not text:
            raise ValueError("Type your answer or question first.")
        if len(text) > MAX_STUDENT_CHARS:
            raise ValueError(f"Messages are limited to {MAX_STUDENT_CHARS} characters.")

        if action == "answer":
            student_text, prompt = text, f"My answer: {text}"
        elif action == "ask":
            student_text, prompt = text, f"My question (answer it, then continue the session): {text}"
        elif action == "mode":
            student_text = f"Switch to {new_mode} mode"
            prompt = f"Switch to {str(new_mode).upper()} mode now and continue the session in that mode."
        else:
            student_text = None if action == "start" else ACTION_TEXT[action]
            prompt = ACTION_TEXT[action]

        messages = [{"role": "system", "content": _system_prompt(session)}]
        for item in history[-HISTORY_MESSAGES:]:
            messages.append({"role": "assistant" if item.role == "tutor" else "user", "content": item.content})
        if session.open_question and action in ("answer", "hint", "skip"):
            prompt = f"(Open question: {session.open_question})\n{prompt}"
        messages.append({"role": "user", "content": prompt})

        raw = self.llm.complete(messages).text
        try:
            reply = _parse(raw)
        except (ValueError, ValidationError):
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": "Reply again with the JSON object only, as described."}]
            raw = self.llm.complete(messages).text
            try:
                reply = _parse(raw)
            except (ValueError, ValidationError):
                reply = TutorReply(message=raw)  # shown as it came, never invented

        verdict = None
        if session.open_question:
            if action == "answer":
                verdict = reply.verdict  # the model's judgement of your answer, counted by Python
            elif action == "skip":
                verdict = "incorrect"
        question = _clip(reply.question, MAX_QUESTION_CHARS)
        tutor_text = compose(reply)
        if question:
            tutor_text = (tutor_text + "\n\n" if tutor_text else "") + f"> **Question:** {question}"
        return TutorTurn(student_text=student_text, tutor_text=tutor_text or "(The tutor sent an empty reply.)",
                         question=question, verdict=verdict)


def take_turn(store: StudyStore, tutor: Tutor, session_id: int, action: str, text: str = "",
              new_mode: str | None = None) -> tuple[TutorTurn, StudySession]:
    """One exchange: ask the tutor, then store it. If the model call fails, nothing
    is stored (except a mode switch you asked for), so you can simply try again."""
    if action == "mode":
        store.set_mode(session_id, new_mode or "")
    session = store.get(session_id)
    turn = tutor.respond(session, store.messages(session_id), action, text, new_mode=new_mode)
    store.touch(session_id)
    if turn.student_text:
        store.add_student(session_id, turn.student_text)
    if turn.verdict:
        store.record_verdict(session_id, turn.verdict)
    store.add_tutor(session_id, turn.tutor_text, turn.question)
    return turn, store.get(session_id)
