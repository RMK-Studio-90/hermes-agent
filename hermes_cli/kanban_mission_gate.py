"""Deterministic mission transition gate for the Kanban kernel.

Phase 1 of persistent mission progression. This module is the pure,
database-free source of truth for WHICH mission transitions are legal.
It performs NO writes and NO side effects — it only answers "is this
transition allowed, and what does the mission look like after it?".

The kernel (``kanban_db``) persists ``waiting_for`` / ``next_transition``
on every ``missions`` row and validates every transition against this gate
before writing (fail-closed: an unknown status, unknown transition, or a
transition not permitted from the current status is REJECTED, never
silently allowed).

Scope: this gate defines the *transition model*. The kernel drives the
lifecycle: auto-remediation (`rework`), auto-re-review, and next-package
progression (`advance` when a PASS has a following package; `complete` when
the sequence ends). This module only makes the legal transitions
deterministic and observable so the kernel can drive them without guessing.

Lifecycle (mission-level, deliberately small and deterministic):

    planned  --start-->  in_progress  --request_review-->  in_review
      |                      ^                                  |
      |                      |            (reviewer decides)    |
      |                      |                |------------------|
      |                      |                | complete        | rework (REVIEW_FAIL)
      |                      |                v                 v
      |                    done  <--------  (complete)   in_progress (loop)
      |                                (advance: PASS, next pkg)  |
      |                                                           |
      +---------------------- unblock/start ---------------------+

`waiting_for` names the precondition the mission is currently awaiting
(which actor/decision unlocks progress). `next_transition` names the single
deterministic transition that becomes legal once that precondition is met
(the "expected next" the orchestrator should attempt). Both are derived
from the status via :data:`MISSION_PHASES` — they are NOT free-text; a
mismatch between status and waiting_for/next_transition is itself treated
as corruption by the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_PLANNED = "planned"
STATUS_IN_PROGRESS = "in_progress"
STATUS_IN_REVIEW = "in_review"
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"

VALID_STATUSES: FrozenSet[str] = frozenset({
    STATUS_PLANNED,
    STATUS_IN_PROGRESS,
    STATUS_IN_REVIEW,
    STATUS_BLOCKED,
    STATUS_DONE,
})

# Terminal statuses: no further transition is legal.
TERMINAL_STATUSES: FrozenSet[str] = frozenset({STATUS_DONE})


# ---------------------------------------------------------------------------
# Transition constants
# ---------------------------------------------------------------------------

TR_START = "start"                # planned -> in_progress
TR_REQUEST_REVIEW = "request_review"  # in_progress -> in_review
TR_COMPLETE = "complete"          # in_review -> done (REVIEW_PASS, sequence end)
TR_REWORK = "rework"              # in_review -> in_progress (REVIEW_FAIL loop)
TR_ADVANCE = "advance"            # in_review -> in_progress (REVIEW_PASS, next package)
TR_BLOCK = "block"                # in_progress|in_review -> blocked
TR_UNBLOCK = "unblock"            # blocked -> in_progress
TR_ABANDON = "abandon"            # blocked/planned -> done (terminal, human)

VALID_TRANSITIONS: FrozenSet[str] = frozenset({
    TR_START,
    TR_REQUEST_REVIEW,
    TR_COMPLETE,
    TR_REWORK,
    TR_ADVANCE,
    TR_BLOCK,
    TR_UNBLOCK,
    TR_ABANDON,
})


# ---------------------------------------------------------------------------
# waiting_for constants
# ---------------------------------------------------------------------------

WAIT_PACKAGE_START = "package_start"      # planned: awaiting first package start
WAIT_PACKAGE_WORK = "package_work"        # in_progress: awaiting implementation
WAIT_REVIEW_DECISION = "review_decision"  # in_review: awaiting reviewer verdict
WAIT_HUMAN = "human"                      # blocked: awaiting human intervention
WAIT_NONE = "none"                        # done: terminal


# ---------------------------------------------------------------------------
# Phase table (single source of truth)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MissionPhase:
    """Deterministic definition of a mission status.

    ``waiting_for`` : the precondition that must be met before the mission
        can leave this status.
    ``next_transition`` : the single canonical transition to attempt once
        ``waiting_for`` is satisfied.
    ``allowed_transitions`` : every transition that is legal FROM this
        status. ``next_transition`` is always a member of this set.
    """

    status: str
    waiting_for: str
    next_transition: str
    allowed_transitions: FrozenSet[str]


# status -> MissionPhase. Unknown status => no phase => fail-closed.
MISSION_PHASES: Dict[str, MissionPhase] = {
    STATUS_PLANNED: MissionPhase(
        status=STATUS_PLANNED,
        waiting_for=WAIT_PACKAGE_START,
        next_transition=TR_START,
        allowed_transitions=frozenset({TR_START, TR_ABANDON}),
    ),
    STATUS_IN_PROGRESS: MissionPhase(
        status=STATUS_IN_PROGRESS,
        waiting_for=WAIT_PACKAGE_WORK,
        next_transition=TR_REQUEST_REVIEW,
        allowed_transitions=frozenset({TR_REQUEST_REVIEW, TR_BLOCK, TR_ABANDON}),
    ),
    STATUS_IN_REVIEW: MissionPhase(
        status=STATUS_IN_REVIEW,
        waiting_for=WAIT_REVIEW_DECISION,
        next_transition=TR_COMPLETE,  # canonical happy-path next; rework/advance also legal
        allowed_transitions=frozenset({
            TR_COMPLETE, TR_REWORK, TR_ADVANCE, TR_BLOCK, TR_ABANDON,
        }),
    ),
    STATUS_BLOCKED: MissionPhase(
        status=STATUS_BLOCKED,
        waiting_for=WAIT_HUMAN,
        next_transition=TR_UNBLOCK,
        allowed_transitions=frozenset({TR_UNBLOCK, TR_ABANDON}),
    ),
    STATUS_DONE: MissionPhase(
        status=STATUS_DONE,
        waiting_for=WAIT_NONE,
        next_transition="",  # terminal: no next
        allowed_transitions=frozenset(),
    ),
}

# Transition -> (from_status, to_status). Single authoritative map so the
# kernel can resolve the target status for a legal transition without
# duplicating the table.
TRANSITION_RESOLUTION: Dict[str, Tuple[str, str]] = {
    TR_START: (STATUS_PLANNED, STATUS_IN_PROGRESS),
    TR_REQUEST_REVIEW: (STATUS_IN_PROGRESS, STATUS_IN_REVIEW),
    TR_COMPLETE: (STATUS_IN_REVIEW, STATUS_DONE),
    TR_REWORK: (STATUS_IN_REVIEW, STATUS_IN_PROGRESS),
    TR_ADVANCE: (STATUS_IN_REVIEW, STATUS_IN_PROGRESS),
    TR_BLOCK: (None, STATUS_BLOCKED),   # from in_progress or in_review
    TR_UNBLOCK: (STATUS_BLOCKED, STATUS_IN_PROGRESS),
    TR_ABANDON: (None, STATUS_DONE),    # from planned, in_progress, in_review, blocked
}


# ---------------------------------------------------------------------------
# Gate API
# ---------------------------------------------------------------------------

def phase_for(status: str) -> MissionPhase:
    """Return the deterministic phase for ``status``.

    Raises ValueError for an unknown status (fail-closed: the kernel must
    never guess a phase for a status it does not recognise).
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown mission status {status!r}; valid: {sorted(VALID_STATUSES)}"
        )
    return MISSION_PHASES[status]


def is_valid_status(status: str) -> bool:
    return status in VALID_STATUSES


def is_valid_transition(transition: str) -> bool:
    return transition in VALID_TRANSITIONS


def can_transition(current_status: str, transition: str) -> bool:
    """Fail-closed legality check WITHOUT raising.

    Returns True only when ``transition`` is a member of the current
    status's ``allowed_transitions``. Unknown status or unknown transition
    => False (never silently allowed).
    """
    if current_status not in VALID_STATUSES:
        return False
    if transition not in VALID_TRANSITIONS:
        return False
    phase = MISSION_PHASES[current_status]
    return transition in phase.allowed_transitions


def resolve_transition(current_status: str, transition: str) -> str:
    """Return the deterministic target status for a legal transition.

    Raises ValueError if the transition is not permitted from
    ``current_status`` (fail-closed). ``block`` and ``abandon`` have
    variable source statuses, so this validates the source explicitly.
    """
    if not can_transition(current_status, transition):
        raise ValueError(
            f"transition {transition!r} is not permitted from mission status "
            f"{current_status!r}; allowed: "
            f"{sorted(MISSION_PHASES[current_status].allowed_transitions)}"
        )
    _from, to_status = TRANSITION_RESOLUTION[transition]
    if _from is not None and _from != current_status:
        raise ValueError(
            f"transition {transition!r} requires source status {_from!r}, "
            f"got {current_status!r}"
        )
    return to_status


def expected_waiting_for(status: str) -> str:
    """Return the deterministic ``waiting_for`` for ``status``."""
    return phase_for(status).waiting_for


def expected_next_transition(status: str) -> str:
    """Return the deterministic ``next_transition`` for ``status``."""
    return phase_for(status).next_transition
