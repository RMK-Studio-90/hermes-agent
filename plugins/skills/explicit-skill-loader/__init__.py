"""Deterministic explicit-skill loader.

Detects an explicit skill request in the user's message (e.g. ``use skill:
plan``, ``verwende skill: obsidian``, ``use the plan skill``) and loads that
skill's full content into the current turn *before* the LLM runs — via the
``pre_llm_call`` plugin context channel. This is deterministic and model-
independent: unlike the skill index in the system prompt (which only lets the
model *choose* to call ``skill_view``), an explicit invocation here is resolved
and loaded by the runtime itself.

Invariants:
- Generic: no per-skill special cases. Any installed skill name works.
- Precedence: an explicit request wins over automatic routing.
- Fail closed: a missing or ambiguous skill produces a clear BLOCK marker and
  never silently continues.
- Does not replace manual/UI (``/skill``), cron (``skills:``), or the
  preloaded (``--skills``/``HERMES_TUI_SKILLS``) paths — those still work.
"""

from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Detection
# ─────────────────────────────────────────────────────────────────────────────
# English and German explicit-skill request forms. These are language-verb
# wrappers around the word "skill" + a name. The captured group is the skill
# name. Patterns are intentionally generic (no per-skill cases).
#
# Supported forms (all generic — no per-skill cases):
#   use skill: plan          use skill plan
#   skill: plan              skill plan
#   use the plan skill       verwende den plan skill
#   nutze skill: obsidian    lade die obsidian skill
#
# Three distinct structural families, kept as separate named branches so a
# self-contained form ("use the plan skill") does not mis-capture a trailing
# word as the skill name:
#   A) verb + [article] + "skill" + separator + NAME   (name AFTER skill)
#   B) bare "skill:" + NAME                            (requires a colon)
#   C) verb + article + NAME + "skill"                 (name BEFORE skill)
_VERB = r"\b(?:use|load|run|invoke|verwende|nutze|lade)\s+"
_ARTICLE = r"(?:(?:the|den|die|das)\s+)?"
_NAME = r"[a-z0-9][\w.\-/+]*"

_SKILL_REQUEST_RE = re.compile(
    rf"""(?ix)
    # A) verb ... skill <sep> NAME
    {_VERB}{_ARTICLE}skill\s*[:,\s]+(?P<name_a>{_NAME})
    |
    # B) bare skill: NAME  (colon required to avoid false positives)
    \bskill\s*:\s*(?P<name_b>{_NAME})
    |
    # C) verb + article + NAME + skill   (self-contained)
    {_VERB}{_ARTICLE}(?P<name_c>{_NAME})\s+skill\b
    """
)

# Strip trailing punctuation / filler that is not part of a skill name.
_TRAILING_RE = re.compile(r"[.,;:!?)\]]+$")


def extract_explicit_skill_request(user_message: Any) -> Optional[str]:
    """Return the requested skill name if the message explicitly requests one.

    Returns ``None`` when the message contains no explicit skill request.
    Detects exactly one request; a message mixing a request with other text
    still resolves the request (the rest of the message is carried through by
    the normal agent flow).
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return None

    # We search the first explicit request. Lowercasing would corrupt
    # camelCase skill names, so match case-insensitively and keep the raw
    # captured name for resolution (resolution normalizes anyway).
    match = _SKILL_REQUEST_RE.search(user_message)
    if not match:
        return None

    name = match.group("name_a") or match.group("name_b") or match.group("name_c")
    if not name:
        return None
    name = _TRAILING_RE.sub("", name).strip()
    return name or None


# ─────────────────────────────────────────────────────────────────────────────
# Resolution (exact, then unambiguous)
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_slug(name: str) -> str:
    """Normalize a skill name to the same slug form ``scan_skill_commands`` uses."""
    slug = name.lower().replace(" ", "-").replace("_", "-")
    slug = re.sub(r"[^a-z0-9\-/]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug


def resolve_skill_identifier(name: str) -> Tuple[str, str]:
    """Resolve *name* to a canonical slash-command key and its display name.

    Returns ``(cmd_key, display_name)`` for an unambiguous match, or raises
    ``SkillResolutionError`` with a clear message for missing / ambiguous
    references. ``cmd_key`` is ``None`` when resolution succeeded by display
    name only (unreachable here — every key carries its display name).
    """
    from agent.skill_commands import get_skill_commands

    commands = get_skill_commands()  # dict: "/slug" -> {name, description, ...}
    if not commands:
        raise SkillResolutionError("No skills are installed; cannot resolve an explicit skill request.")

    request_slug = _normalize_slug(name)

    # 1. Exact slug match (canonical).
    exact_key = f"/{request_slug}"
    if exact_key in commands:
        return exact_key, commands[exact_key].get("name") or request_slug

    # 2. Exact display-name match (case/space/underscore tolerant).
    for key, info in commands.items():
        if _normalize_slug(info.get("name", "")) == request_slug:
            return key, info.get("name") or request_slug

    # 3. Unambiguous substring/prefix candidate set.
    candidates: List[Tuple[str, str]] = []
    for key, info in commands.items():
        display = info.get("name", "")
        slug = _normalize_slug(display)
        if request_slug in slug or slug in request_slug or request_slug in key:
            candidates.append((key, display))

    if not candidates:
        raise SkillResolutionError(
            f"Skill '{name}' does not exist. No installed skill matches. "
            "Use skills_list to see available skills."
        )
    if len(candidates) > 1:
        names = ", ".join(sorted({d for _, d in candidates}))
        raise SkillResolutionError(
            f"Skill reference '{name}' is ambiguous — matches: {names}. "
            "Use an exact skill name."
        )
    return candidates[0]


class SkillResolutionError(Exception):
    """Raised when an explicit skill reference cannot be resolved uniquely."""


# ─────────────────────────────────────────────────────────────────────────────
# Load
# ─────────────────────────────────────────────────────────────────────────────
def build_explicit_skill_context(
    user_message: Any,
    *,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Load the requested skill and return the ``pre_llm_call`` context dict body.

    Returns the context *string* to inject (already the full activation
    message), or ``None`` when there is no explicit skill request. On a
    missing/ambiguous skill it returns a clear BLOCK marker instead of
    ``None`` so the turn fails closed.
    """
    from agent.skill_commands import build_skill_invocation_message

    name = extract_explicit_skill_request(user_message)
    if name is None:
        return None

    try:
        cmd_key, display_name = resolve_skill_identifier(name)
    except SkillResolutionError as exc:
        logger.warning("explicit-skill-loader: %s", exc)
        return _block_marker(str(exc))

    # Reuse the exact same loader the /skill slash command uses, so the
    # loaded payload, activation note, supporting-file list, and config
    # injection are byte-identical to the manual/UI path.
    loaded = build_skill_invocation_message(cmd_key, task_id=task_id)
    if not loaded:
        return _block_marker(
            f"Skill '{display_name}' could not be loaded (load error). "
            "Fix the skill or use a different one."
        )
    return loaded


def _block_marker(reason: str) -> str:
    """A clear, deterministic fail-closed marker injected into the turn."""
    return (
        "[BLOCKED / DO NOT EXECUTE — explicit skill could not be loaded]\n"
        f"REASON: {reason}\n"
        "Do not proceed with the requested task. Report this blocker to the "
        "user and stop."
    )


# ─────────────────────────────────────────────────────────────────────────────
# pre_llm_call hook entry point
# ─────────────────────────────────────────────────────────────────────────────
def on_pre_llm_call(
    *,
    user_message: Any = None,
    task_id: Optional[str] = None,
    **_: Any,
) -> Optional[dict]:
    """pre_llm_call handler: inject the requested skill's content pre-LLM."""
    context = build_explicit_skill_context(user_message, task_id=task_id)
    if context is None:
        return None
    return {"context": context}


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
