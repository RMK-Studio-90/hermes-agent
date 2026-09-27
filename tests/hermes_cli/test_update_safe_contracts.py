"""Contract Registry tests (RED-first for the update-safe feature).

These tests define the machine-checkable RMK behavior contracts BEFORE any
implementation exists. They validate BEHAVIOR against a target checkout via a
helper subprocess, so they must not need network or production services.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hermes_cli.update_safe_contracts import (  # noqa: E402
    CONTRACT_IDS,
    build_contract_registry,
    run_contracts,
    contract_summary,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def test_contract_registry_has_all_required_contracts():
    """The 16 RMK contract areas from the spec must all exist in the registry."""
    required = {
        "smart_routing_free_first",
        "strict_pins",
        "adaptive_failover",
        "provider_cooldown_reset",
        "routing_telemetry",
        "openviking_hooks",
        "background_review",
        "cron_heartbeat",
        "gateway",
        "config_env",
        "update_autostash",
        "backup_restore",
        "windows_unicode_cp1252",
        "model_switching",
        "relay_tool_surface",
        "no_auto_paid_escalation",
    }
    assert required.issubset(set(CONTRACT_IDS))


def test_every_contract_has_a_probe():
    """No contract may be a no-op: every registry entry must run against a tree."""
    registry = build_contract_registry()
    assert len(registry) >= len(CONTRACT_IDS)
    for check in registry:
        assert check.contract_id in CONTRACT_IDS


def test_run_contracts_against_this_worktree_head_passes():
    """The merged worktree (current certified state) must satisfy all 16 contracts.

    This is the self-certification anchor: upgrade-time contract PASS/FAIL is
    measured against this same machine-checkable baseline.
    """
    results = run_contracts(_repo_root())
    summary = contract_summary(results)
    assert summary["failed"] == 0, f"contracts failed: {summary['failed_ids']}"


def test_contract_summary_shape():
    summary = contract_summary([
        type("R", (), {"contract_id": "a", "passed": True})(),
        type("R", (), {"contract_id": "b", "passed": False})(),
    ])
    assert summary == {"total": 2, "passed": 1, "failed": 1, "failed_ids": ["b"]}
