"""Prepared MoA transcripts must honor destination transport contracts."""
from copy import deepcopy
from types import SimpleNamespace


def test_prepared_chat_strips_internal_fields_preserves_memory_pair(monkeypatch):
    from agent import moa_loop

    messages = [
        {"role": "user", "content": "Recall my identifier", "timestamp": 123.4, "_db_persisted": True},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "memory_1", "type": "function", "function": {
                "name": "second_brain_recall", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "memory_1", "name": "second_brain_recall",
         "content": "verified memory", "timestamp": 124.4},
    ]
    original = deepcopy(messages)
    captured = {}
    monkeypatch.setattr(moa_loop, "_slot_runtime", lambda slot: {
        "provider": "custom", "model": "test", "api_mode": "chat_completions"})
    def call(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[])
    monkeypatch.setattr(moa_loop, "call_llm", call)
    facade = moa_loop.MoAChatCompletions("test")
    monkeypatch.setattr(facade, "_plan_aggregator_cache", lambda msgs, tools, guidance, runtime: (msgs, tools))
    facade._call_prepared_aggregator({"aggregator": {"provider": "custom", "model": "test"},
        "messages": messages, "aggregator_temperature": 0}, {})
    assert messages == original
    assert all("timestamp" not in m and "_db_persisted" not in m for m in captured["messages"])
    assert captured["messages"][1]["tool_calls"] == original[1]["tool_calls"]
    assert captured["messages"][2]["tool_call_id"] == "memory_1"
    assert captured["messages"][2]["content"] == "verified memory"
