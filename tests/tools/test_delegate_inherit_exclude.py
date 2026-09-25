"""Implicit toolset inheritance skips the parent's human-facing toolsets.

A delegated child's only audience is its parent, so ``tts`` (speaks to the
user) and ``session_search`` (recalls the user's past conversations) are
dropped when the child inherits toolsets implicitly. Configurable via
``delegation.inherit_exclude_toolsets``; explicit toolsets are honored.
"""
from __future__ import annotations

import pytest


class _Parent:
    enabled_toolsets = ["terminal", "file", "web", "tts", "session_search", "todo"]
    valid_tool_names = {
        "terminal", "read_file", "web_search", "text_to_speech",
        "session_search", "todo",
    }
    model = "test-model"
    provider = "test-provider"
    base_url = "http://example.invalid"
    api_mode = "chat_completions"
    platform = "cli"
    session_id = "parent-session"


def _build(monkeypatch, cfg, toolsets=None):
    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.valid_tool_names = {"terminal"}
            self.session_id = "child-session"

    import run_agent
    from tools import delegate_tool

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: cfg)
    delegate_tool._build_child_agent(
        task_index=0,
        goal="inspect",
        context=None,
        toolsets=toolsets,
        model=None,
        max_iterations=3,
        task_count=1,
        parent_agent=_Parent(),
    )
    return captured


def test_implicit_inheritance_denies_parent_surface_toolsets(monkeypatch):
    captured = _build(monkeypatch, {})
    assert {"tts", "session_search"} <= set(captured["disabled_toolsets"])
    # Worker capabilities the parent has are still inherited.
    assert {"terminal", "file", "web", "todo"} <= set(captured["enabled_toolsets"])


def test_empty_config_list_restores_full_inheritance(monkeypatch):
    captured = _build(monkeypatch, {"inherit_exclude_toolsets": []})
    assert "tts" not in captured["disabled_toolsets"]
    assert "session_search" not in captured["disabled_toolsets"]


def test_configured_list_replaces_defaults(monkeypatch):
    captured = _build(monkeypatch, {"inherit_exclude_toolsets": ["todo"]})
    assert "todo" in captured["disabled_toolsets"]
    assert "tts" not in captured["disabled_toolsets"]


def test_explicit_toolsets_are_honored(monkeypatch):
    captured = _build(monkeypatch, {}, toolsets=["session_search", "terminal"])
    assert "session_search" in captured["enabled_toolsets"]
    assert "session_search" not in captured["disabled_toolsets"]


@pytest.mark.parametrize("composite", ["hermes-cli", "hermes-telegram"])
def test_deny_toolsets_subtract_inside_composite_bundles(composite):
    """The exclusion rides disabled_toolsets so it also bites when the parent
    (and therefore the child) runs a composite platform bundle."""
    from model_tools import get_tool_definitions

    names = {
        (t.get("function") or {}).get("name")
        for t in get_tool_definitions(
            enabled_toolsets=[composite],
            disabled_toolsets=["tts", "session_search"],
            quiet_mode=True,
        )
    }
    assert "text_to_speech" not in names
    assert "session_search" not in names
    assert "read_file" in names
