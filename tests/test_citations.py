"""The citations rule: a cited source must have been read in this request."""

import json

import pytest

from app.agent.loop import AgentLoop
from app.audit import AuditLog
from app.citations import annotate, citations_in, normalize, unverified
from app.config import AutonomyLevel, Settings
from app.llm.client import FakeLLMClient
from app.memory.documents import Chunk, DocumentStore
from app.safety.permissions import DEFAULT_GRANTS, SafetyLayer
from app.tools.base import ToolContext
from app.tools.builtin import build_default_registry
from app.tools.docs import register_doc_tools
from app.tools.web import register_web_tools

# --- The pure functions -----------------------------------------------------------------------


def test_only_citation_shaped_brackets_count():
    text = ("Joins combine tables [dbms/lecture3.pdf, p. 2]; see also [notes.md, line 40] and "
            "[https://en.wikipedia.org/wiki/Join]. This is [optional] and a [link](https://x.org).")
    assert citations_in(text) == ["dbms/lecture3.pdf, p. 2", "notes.md, line 40", "https://en.wikipedia.org/wiki/Join"]


def test_several_citations_in_one_bracket():
    assert citations_in("[a.pdf, p. 1; b.pdf, p. 2]") == ["a.pdf, p. 1", "b.pdf, p. 2"]


@pytest.mark.parametrize(
    ("cited", "source"),
    [
        ("dbms/lecture3.pdf, p.2", "dbms/lecture3.pdf, p. 2"),
        ("DBMS/Lecture3.pdf,  p. 2", "dbms/lecture3.pdf, p. 2"),
        ("notes.md, line  40", "notes.md, line 40"),
        ("https://EN.wikipedia.org/wiki/Join/", "https://en.wikipedia.org/wiki/Join"),
        ("https://en.wikipedia.org/wiki/Join#History", "https://en.wikipedia.org/wiki/Join"),
    ],
)
def test_harmless_formatting_differences_still_match(cited, source):
    assert normalize(cited) == normalize(source)
    assert unverified(f"[{cited}]", {source}) == []


def test_invented_sources_are_flagged():
    text = "As shown in [dbms/lecture9.pdf, p. 4] and [https://made-up.example/paper]."
    assert unverified(text, {"dbms/lecture3.pdf, p. 2"}) == ["dbms/lecture9.pdf, p. 4", "https://made-up.example/paper"]


def test_annotation():
    assert annotate("Answer.", []) == "Answer."
    assert annotate("Answer.", ["x.pdf, p. 1"]) == (
        "Answer.\n\n⚠ Unverified source(s), not read in this request: [x.pdf, p. 1]")


# --- Through the agent loop ---------------------------------------------------------------------


def _call(tool, args):
    return json.dumps({"action": "tool_call", "tool": tool, "args": args, "summary": "Research."})


def _reply(text):
    return json.dumps({"action": "reply", "text": text, "summary": "Answered."})


class _Transport:
    def __call__(self, host, ip, path, timeout):
        return 200, {"Content-Type": "text/html"}, b"<title>Join</title><p>A join combines rows.</p>"


class _Env:
    def __init__(self, tmp_path, replies):
        ws = tmp_path / "ws"
        (ws / "library").mkdir(parents=True)
        registry = build_default_registry()
        register_doc_tools(registry, ws)
        register_web_tools(registry, ("wikipedia.org",), resolver=lambda host: ["93.184.216.34"],
                           transport=_Transport())
        self.docs = DocumentStore(tmp_path / "agent.db")
        self.docs.save("dbms/lecture3.pdf", "pdf", "h", 1, 2,
                       [Chunk(page=1, line=None, text="relational algebra"), Chunk(page=2, line=None, text="joins")])
        self.audit = AuditLog(tmp_path / "audit.jsonl")
        self.llm = FakeLLMClient(replies=replies)
        self.agent = AgentLoop(
            settings=Settings(openai_api_key="k", openai_model="m", autonomy_level=AutonomyLevel.AUTO_SAFE),
            llm=self.llm,
            registry=registry,
            safety=SafetyLayer(registry, allowed_permissions=DEFAULT_GRANTS, autonomy=AutonomyLevel.AUTO_SAFE),
            tool_ctx=ToolContext(doc_store=self.docs),
            audit=self.audit,
        )


def test_citing_what_was_read_passes(tmp_path):
    env = _Env(tmp_path, [
        _call("doc_search", {"query": "joins"}),
        _call("web_fetch", {"url": "https://en.wikipedia.org/wiki/Join"}),
        _reply("Joins combine rows [dbms/lecture3.pdf, p. 2] [https://en.wikipedia.org/wiki/Join]."),
    ])
    run = env.agent.run("what is a join?")
    assert run.unverified_citations == []
    assert "Unverified" not in run.final_text


def test_invented_citations_are_flagged_for_the_user(tmp_path):
    """Security: an invented source is visible as such, not presented as fact."""
    env = _Env(tmp_path, [
        _call("doc_search", {"query": "joins"}),
        _reply("Joins combine rows [dbms/lecture3.pdf, p. 2], first proposed in [dbms/lecture9.pdf, p. 7] "
               "and [https://en.wikipedia.org/wiki/Join]."),
    ])
    run = env.agent.run("what is a join?")
    assert run.unverified_citations == ["dbms/lecture9.pdf, p. 7", "https://en.wikipedia.org/wiki/Join"]
    assert run.final_text.endswith(
        "⚠ Unverified source(s), not read in this request: [dbms/lecture9.pdf, p. 7]; "
        "[https://en.wikipedia.org/wiki/Join]")
    end = [e for e in env.audit.read() if e["event"] == "run_end"][0]
    assert end["unverified_citations"] == 2
    assert "lecture9" not in env.audit.path.read_text(encoding="utf-8")  # count only, no text


def test_a_hit_found_on_another_page_is_not_a_source_for_every_page(tmp_path):
    env = _Env(tmp_path, [_call("doc_search", {"query": "joins"}), _reply("See [dbms/lecture3.pdf, p. 1].")])
    run = env.agent.run("joins?")
    assert run.unverified_citations == ["dbms/lecture3.pdf, p. 1"]  # only p. 2 was a hit


def test_sources_from_an_earlier_request_do_not_count(tmp_path):
    from app.conversation import Conversation

    env = _Env(tmp_path, [_call("doc_search", {"query": "joins"}), _reply("ok [dbms/lecture3.pdf, p. 2]"),
                          _reply("As I said [dbms/lecture3.pdf, p. 2].")])
    conv = Conversation()
    assert env.agent.run("first", conv).unverified_citations == []
    assert env.agent.run("second", conv).unverified_citations == ["dbms/lecture3.pdf, p. 2"]


def test_the_rule_is_in_the_system_prompt(tmp_path):
    env = _Env(tmp_path, [_reply("hi")])
    env.agent.run("hi")
    assert "Cite only sources you read in this request." in env.llm.calls[0][0]["content"]


def test_answers_without_citations_are_untouched(tmp_path):
    env = _Env(tmp_path, [_reply("A join combines rows [roughly speaking].")])
    run = env.agent.run("join?")
    assert run.final_text == "A join combines rows [roughly speaking]."
