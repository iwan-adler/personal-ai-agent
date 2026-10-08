from app.conversation import Conversation
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
