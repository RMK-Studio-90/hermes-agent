"""Regression tests for the bundled explicit-skill-loader plugin.

Covers:
- deterministic parsing of explicit skill requests (EN + DE, several forms)
- resolution against REAL installed skills (plan, obsidian)
- missing-skill fail-closed
- ambiguity fail-closed
- the pre_llm_call hook returns the full activation context

The plugin is loaded exactly the way the Hermes plugin manager loads a
bundled directory plugin: as ``hermes_plugins.<slug>`` via ``importlib``.

The test-suite conftest sandboxes ``HERMES_HOME`` to an empty tempdir, so the
"real skills" resolution/loading cases restore the production home and force a
fresh skill scan, then tear it down. Missing/ambiguity/parsing cases are
deterministic and run in the sandbox.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "skills" / "explicit-skill-loader"
SLUG = "hermes_plugins.skills__explicit_skill_loader"
REAL_HOME = r"E:\KI\Hermes"


def _load_plugin():
    """Load the plugin module the way PluginManager._load_directory_module does."""
    if SLUG in sys.modules:
        return sys.modules[SLUG]
    init_file = PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        SLUG,
        init_file,
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    module.__package__ = SLUG
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[SLUG] = module
    spec.loader.exec_module(module)
    return module


_plugin = _load_plugin()
extract_explicit_skill_request = _plugin.extract_explicit_skill_request
build_explicit_skill_context = _plugin.build_explicit_skill_context
on_pre_llm_call = _plugin.on_pre_llm_call
resolve_skill_identifier = _plugin.resolve_skill_identifier
SkillResolutionError = _plugin.SkillResolutionError
_block_marker = _plugin._block_marker
register = _plugin.register


@pytest.fixture
def real_home(monkeypatch):
    """Point HERMES_HOME at the production base and force a fresh skill scan."""
    monkeypatch.setenv("HERMES_HOME", REAL_HOME)
    # Clear any cached scan so get_skill_commands() rescans under the real home.
    from agent import skill_commands as sc
    with mock.patch.object(sc, "_skill_commands", {}):
        sc._skill_commands_platform = None
        sc._skill_commands_home = None
        yield


# ─────────────────────────────────────────────────────────────────────────────
# Parsing (deterministic, no model involved)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "message,expected",
    [
        ("Use skill: plan", "plan"),
        ("use skill plan", "plan"),
        ("skill: plan", "plan"),
        ("use the plan skill", "plan"),
        ("verwende skill: plan", "plan"),
        ("nutze skill: plan", "plan"),
        ("Use skill: obsidian", "obsidian"),
        ("Bitte verwende den obsidian skill", "obsidian"),
        ("load the obsidian skill and summarize", "obsidian"),
        # name with underscore/hyphen/dot survives trailing punctuation
        ("Use skill: my-skill.", "my-skill"),
        ("Use skill: ai_tool", "ai_tool"),
    ],
)
def test_extracts_skill_name(message, expected):
    assert extract_explicit_skill_request(message) == expected


@pytest.mark.parametrize(
    "message",
    [
        "",
        "What is the capital of France?",
        "Summarize this plan.",
        "I have a skill question",
        "Plan the release",  # "plan" as a verb, not a skill request
        "Let's plan the migration",  # not an explicit skill invocation
    ],
)
def test_no_false_positive(message):
    assert extract_explicit_skill_request(message) is None


# ─────────────────────────────────────────────────────────────────────────────
# Stopword / false-positive guard regression (W1 hardening)
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "message",
    [
        # Natural-language "skill" mentions must NOT extract a stopword
        # (preposition/article) as the skill name — previously these fell
        # through to a spurious fail-closed BLOCK.
        "use the skill of active listening",
        "use the skill for planning",
        "I would like to use the skill of active listening",
        "use the skill with care",
    ],
)
def test_natural_language_skill_phrase_no_false_positive(message):
    assert extract_explicit_skill_request(message) is None


@pytest.mark.parametrize(
    "message,expected",
    [
        # The stopword guard must not break valid self-contained forms.
        ("use the plan skill", "plan"),
        ("Please use the plan skill now", "plan"),
    ],
)
def test_stopword_guard_keeps_valid_plan_skill(message, expected):
    assert extract_explicit_skill_request(message) == expected


# ─────────────────────────────────────────────────────────────────────────────
# Resolution against REAL skills (production home restored)
# ─────────────────────────────────────────────────────────────────────────────
def test_resolve_plan_real(real_home):
    key, display = resolve_skill_identifier("plan")
    assert key == "/plan"
    assert display == "plan"


def test_resolve_obsidian_real(real_home):
    key, display = resolve_skill_identifier("obsidian")
    assert key == "/obsidian"
    assert display == "obsidian"


def test_resolve_normalizes_case_and_underscore(real_home):
    key, _ = resolve_skill_identifier("Obsidian")
    assert key == "/obsidian"


def test_resolve_missing_fails_closed():
    with pytest.raises(SkillResolutionError) as ei:
        resolve_skill_identifier("definitely-not-a-real-skill-xyz")
    assert "does not exist" in str(ei.value)


def test_resolve_ambiguous_fails_closed():
    # "obs" is a prefix of many skill names -> ambiguous. Inject a fixed map
    # to make the ambiguity deterministic regardless of install set.
    fake = {
        "/obsidian": {"name": "obsidian"},
        "/obsidian-export": {"name": "obsidian-export"},
        "/other": {"name": "other"},
    }
    # The plugin imports get_skill_commands lazily inside the resolver, so
    # patch the source module it imports from.
    with mock.patch("agent.skill_commands.get_skill_commands", return_value=fake):
        with pytest.raises(SkillResolutionError) as ei:
            resolve_skill_identifier("obs")
    assert "ambiguous" in str(ei.value)


# ─────────────────────────────────────────────────────────────────────────────
# Context building + hook
# ─────────────────────────────────────────────────────────────────────────────
def test_build_context_plan_real(real_home):
    ctx = build_explicit_skill_context("Use skill: plan. Do not write a plan.")
    assert ctx is not None
    assert "plan" in ctx
    assert "[IMPORTANT: The user has invoked" in ctx


def test_build_context_obsidian_real(real_home):
    ctx = build_explicit_skill_context("verwende skill: obsidian")
    assert ctx is not None
    assert "obsidian" in ctx


def test_build_context_no_request_returns_none():
    assert build_explicit_skill_context("Just summarize this doc.") is None


def test_build_context_missing_returns_block_marker():
    ctx = build_explicit_skill_context("Use skill: definitely-not-real-xyz")
    assert ctx is not None
    assert "BLOCKED / DO NOT EXECUTE" in ctx
    assert "does not exist" in ctx


def test_hook_returns_context_dict_for_request(real_home):
    result = on_pre_llm_call(user_message="Use skill: plan")
    assert isinstance(result, dict)
    assert "context" in result
    assert "plan" in result["context"]


def test_hook_returns_none_without_request():
    assert on_pre_llm_call(user_message="Hello there") is None


def test_block_marker_content():
    m = _block_marker("reason text")
    assert "BLOCKED / DO NOT EXECUTE" in m
    assert "reason text" in m
    assert "Report this blocker" in m


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────
def test_register_wires_pre_llm_call_hook():
    ctx = mock.MagicMock()
    register(ctx)
    ctx.register_hook.assert_called_once_with("pre_llm_call", on_pre_llm_call)
