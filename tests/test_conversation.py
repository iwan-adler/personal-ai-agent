import pytest

from app.conversation import SYSTEM_PROMPT, Conversation
from app.llm.client import FakeLLMClient


def test_conversation_stores_turns():
    conv = Conversation()
    conv.add_user("hello")
    conv.add_assistant("hi")
    roles = [m["role"] for m in conv.messages]
    assert roles[0] == "system"
    assert conv.turns() == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


def test_fake_llm_never_uses_network():
    fake = FakeLLMClient(replies=["one", "two"])
    first = fake.complete([{"role": "user", "content": "a"}])
    second = fake.complete([{"role": "user", "content": "b"}])
    assert first.text == "one"
    assert second.text == "two"
    assert len(fake.calls) == 2


# --- The window sent to the model ---------------------------------------------------



def _turn(conv: Conversation, n: int, tool: bool = False) -> None:
    conv.add_user(f"question {n}")
    if tool:
        conv.add_assistant(f'{{"action": "tool_call", "n": {n}}}')
        conv.add_user(f"<tool_result>{n}</tool_result>", kind="tool_result")
    conv.add_assistant(f"answer {n}")


def test_short_history_is_sent_whole():
    conv = Conversation()
    _turn(conv, 1)
    assert conv.window(20) == conv.messages


def test_window_keeps_the_system_prompt_and_the_latest_messages():
    conv = Conversation()
    for n in range(1, 21):
        _turn(conv, n)  # 40 messages
    window = conv.window(6)
    assert window[0]["content"] == SYSTEM_PROMPT
    assert [m["content"] for m in window[1:]] == [
        "question 18", "answer 18", "question 19", "answer 19", "question 20", "answer 20",
    ]


def test_window_never_starts_with_a_tool_result_or_repair():
    conv = Conversation()
    for n in range(1, 6):
        _turn(conv, n, tool=True)  # 4 messages per turn
    conv.add_user("Your last reply could not be used", kind="repair")
    for size in range(4, 22):
        window = conv.window(size)
        first = window[1]
        assert first["content"].startswith("question"), (size, first)


def test_current_request_is_never_cut():
    conv = Conversation()
    _turn(conv, 1)
    conv.add_user("long request")
    for n in range(10):  # a run longer than the window
        conv.add_assistant(f"call {n}")
        conv.add_user(f"<tool_result>{n}</tool_result>", kind="tool_result")
    window = conv.window(4)
    assert window[1]["content"] == "long request"
    assert window[-1]["content"] == "<tool_result>9</tool_result>"


def test_kinds_must_match_messages():
    with pytest.raises(ValueError):
        Conversation(messages=[{"role": "system", "content": "x"}], kinds=["system", "user"])
