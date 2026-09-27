"""Baseline-Differential for the update-safe certification gate.

A certified state records the test fingerprint (PASS/FAIL/SKIP per suite) and
the contract summary. The next update compares its own fingerprint against the
stored one so KNOWN_BASELINE_FAILURE (a historical red suite, e.g.
``memory_scope: 11 FAIL``) is never mistaken for a new REGRESSION.

Verbatim from the spec:
    vorher:  memory_scope: 11 FAIL
    nachher: memory_scope: 11 FAIL  -> no new regression
    nachher: memory_scope: 12 FAIL  -> block the update
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

#: suite -> (passed, failed) counts, or suite -> (passed, failed, skipped)
Baseline = Dict[str, Tuple[int, int, int]]


def _triple(value: Any) -> Tuple[int, int, int]:
    """Normalize any stored shape to (passed, failed, skipped)."""
    if isinstance(value, (list, tuple)):
        parts = list(value)
        while len(parts) < 3:
            parts.append(0)
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    raise TypeError(f"cannot normalize baseline entry: {value!r}")


def normalize_baseline(data: Dict[str, Any]) -> Baseline:
    return {suite: _triple(cnt) for suite, cnt in (data or {}).items()}


def diff_baseline(old: Baseline | Dict[str, Any],
                  new: Baseline | Dict[str, Any]) -> Dict[str, Any]:
    """Compare two fingerprints.

    Returns dict: verdict in {KNOWN_BASELINE_FAILURE, REGRESSION, IDENTICAL,
    IMPROVED, NO_SUITES}, new_regressions (suites whose FAIL count rose above
    the stored baseline), resolved (suites whose FAIL count fell), vanished.
    Rules (spec): identical FAIL counts -> KNOWN_BASELINE_FAILURE (historical
    reds stay known; they are never a new regression); 11 -> 12 FAIL is a
    REGRESSION. An all-green identical fingerprint is IDENTICAL.
    """
    old_n = normalize_baseline(old)
    new_n = normalize_baseline(new)
    new_regressions: List[str] = []
    resolved: List[str] = []
    for suite, (n_pass, n_fail, n_skip) in new_n.items():
        old_t = old_n.get(suite, (0, 0, 0))
        if n_fail > old_t[1]:
            new_regressions.append(suite)
        elif n_fail < old_t[1]:
            resolved.append(suite)
    vanished = [s for s in old_n if s not in new_n]
    if new_regressions:
        verdict = "REGRESSION"
    elif not new_n:
        verdict = "NO_SUITES"
    elif not old_n:
        # First certification — nothing stored yet; the new state is the baseline.
        verdict = "KNOWN_BASELINE_FAILURE" if any(t[1] > 0 for t in new_n.values()) else "IDENTICAL"
    elif new_n == old_n:
        verdict = "KNOWN_BASELINE_FAILURE" if any(t[1] > 0 for t in new_n.values()) else "IDENTICAL"
    elif resolved or vanished:
        verdict = "IMPROVED"
    else:
        verdict = "KNOWN_BASELINE_FAILURE"
    return {
        "verdict": verdict,
        "new_regressions": new_regressions,
        "resolved": resolved,
        "vanished": vanished,
        "old": {k: list(v) for k, v in old_n.items()},
        "new": {k: list(v) for k, v in new_n.items()},
    }


def save_baseline(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)  # atomic on Windows within the same dir


def load_baseline(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("baseline %s unreadable: %s", path, exc)
        return {}
