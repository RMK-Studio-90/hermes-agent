"""RMK Contract Registry — machine-checkable RMK behavior contracts.

Every contract is a function that returns a :class:`ContractResult`. The checks
run against a *target checkout* (an isolated update worktree or the certified
production tree) via a helper subprocess, so they validate behavior and
importability of the NEW tree without loading it into this interpreter.

Contracts pin BEHAVIOR, not implementation details: the check is "module X
imports + anchor Y exists + semantics Z holds", so an upstream refactor that
keeps the behavior natively passes without re-pinning old names.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

CONTRACT_IDS = [
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
]

_PY = sys.executable
#: placeholder replaced (NOT .format) so probe code may contain braces
_TARGET = "__TARGET_ROOT__"


@dataclass(frozen=True)
class ContractResult:
    contract_id: str
    passed: bool
    detail: str = ""
    error: Optional[str] = None


@dataclass
class ContractProbe:
    """One behavioral probe executed in a subprocess against the target tree."""

    contract_id: str
    probe_code: str
    description: str = ""

    def run(self, target_root: Path) -> ContractResult:
        code = self.probe_code.replace(_TARGET, repr(str(target_root)))
        cmd = [_PY, "-c", code]
        env = dict(os.environ, PYTHONPATH=str(target_root))
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=90, cwd=str(target_root), env=env,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ContractResult(self.contract_id, False, "probe crashed", str(exc))
        if r.returncode == 0:
            return ContractResult(self.contract_id, True, r.stdout.strip() or "OK")
        detail = (r.stderr or r.stdout).strip()
        return ContractResult(self.contract_id, False, detail[:300], detail[:300])


def _imp(module: str, *anchors: str, extra: str = "") -> str:
    """Probe: import ``module`` and assert every anchor exists (all must be present)."""
    lines = ["import importlib", f"m = importlib.import_module({module!r})"]
    for a in anchors:
        lines.append(f"assert hasattr(m, {a!r}), {('missing ' + module + '::' + a)!r}")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _import_only(module: str) -> str:
    return f"import {module}\n"


def _tree_marker(marker: str, subdir: str = "") -> str:
    """Probe: at least one *.py file under target_root[/subdir] contains the marker."""
    if subdir:
        sub_src = f"root = pathlib.Path({_TARGET}) / {subdir!r}"
    else:
        sub_src = f"root = pathlib.Path({_TARGET})"
    return (
        "import pathlib\n"
        + sub_src + "\n"
        + f"marker = {marker!r}.lower()\n"
        + "hits = []\n"
        + "for p in root.rglob('*.py'):\n"
        + "    if '.git' in p.parts or '__pycache__' in p.parts:\n"
        + "        continue\n"
        + "    try:\n"
        + "        text = p.read_text(encoding='utf-8', errors='replace')\n"
        + "    except OSError:\n"
        + "        continue\n"
        + "    if marker in text.lower():\n"
        + "        hits.append(str(p))\n"
        + "assert hits, f'marker not found in ' + str(root)\n"
        + "print('hits=' + str(len(hits)))\n"
    )


def build_contract_registry() -> List[ContractProbe]:
    """The RMK Contract Registry — every RMK-specific behavior an update must keep."""
    return [
        ContractProbe(
            "smart_routing_free_first",
            _imp("hermes_cli.config",
                 extra="keys = getattr(m, '_KNOWN_ROOT_KEYS', []) or []\n"
                       "assert 'smart_model_routing' in keys or 'smart_model_routing' in dir(m) or "
                       "'smart_model_routing' in str(getattr(m, 'DEFAULT_CONFIG', {}))\n"),
            "config carries the smart_model_routing key (setup wizard writes it)",
        ),
        ContractProbe(
            "strict_pins",
            _imp("hermes_cli.config", "DEFAULT_CONFIG",
                 extra="assert 'model' in m.DEFAULT_CONFIG\n"),
            "strict-pin capable config defaults load",
        ),
        ContractProbe(
            "adaptive_failover",
            _import_only("agent.chat_completion_helpers") +
            "from agent.routing.integration import reorder_fallback_chain\n"
            "from agent.fallback_cooldown import _arm_rate_limit_cooldown, switch_deferred_by_reset\n",
            "adaptive failover chain reorder + deferred-by-reset switch",
        ),
        ContractProbe(
            "provider_cooldown_reset",
            _imp("agent.fallback_cooldown", "_arm_rate_limit_cooldown", "switch_deferred_by_reset"),
            "provider cooldown arming + reset-based deferral",
        ),
        ContractProbe(
            "routing_telemetry",
            _import_only("agent.routing") + _import_only("agent.routing.integration")
            + "import agent.routing.integration as ri\n"
            + "assert hasattr(ri, 'reorder_fallback_chain')\n",
            "routing package with integration telemetry surface",
        ),
        ContractProbe(
            "openviking_hooks",
            _tree_marker("openviking"),
            "OpenViking/external-memory hook marker present in the tree",
        ),
        ContractProbe(
            "background_review",
            _imp("agent.background_review", "_review_tool_whitelist", "_review_input_token_budget"),
            "background review whitelist + budget resolution",
        ),
        ContractProbe(
            "cron_heartbeat",
            _imp("cron.scheduler", "heartbeat_fire_claim", "heartbeat_run_claim"),
            "cron scheduler heartbeat claims importable",
        ),
        ContractProbe(
            "gateway",
            _import_only("hermes_cli.gateway"),
            "gateway facade imports",
        ),
        ContractProbe(
            "config_env",
            _import_only("hermes_cli.env_loader"),
            "env loader imports",
        ),
        ContractProbe(
            "update_autostash",
            _imp("hermes_cli.update_cmd_stash", "_stash_local_changes_if_needed", "_restore_stashed_changes"),
            "autostash park/restore surface",
        ),
        ContractProbe(
            "backup_restore",
            _import_only("hermes_cli.backup_restore") +
            "import hermes_cli.backup as bk\n"
            "assert hasattr(bk, 'create_quick_snapshot')\n"
            "assert hasattr(bk, 'restore_quick_snapshot')\n",
            "backup/restore quick-snapshot surface",
        ),
        ContractProbe(
            "windows_unicode_cp1252",
            _imp("hermes_cli.env_loader",
                 extra="import inspect\n"
                       "src = inspect.getsource(m)\n"
                       "assert 'UnicodeDecodeError' in src or 'unicode' in src.lower()\n"),
            "env loader guards cp1252/UnicodeDecodeError",
        ),
        ContractProbe(
            "model_switching",
            _import_only("agent.routing._flags"),
            "model-switch flag module importable",
        ),
        ContractProbe(
            "relay_tool_surface",
            _import_only("agent.relay_cwd"),
            "relay cwd module importable",
        ),
        ContractProbe(
            "no_auto_paid_escalation",
            _tree_marker("free_first", "agent/routing"),
            "agent/routing carries the free-first escalation marker",
        ),
    ]


def run_contracts(target_root: Path, contract_ids: Optional[List[str]] = None) -> List[ContractResult]:
    """Run the registry (or a subset) against ``target_root``. Never raises."""
    registry = build_contract_registry()
    results: List[ContractResult] = []
    for probe in registry:
        if contract_ids and probe.contract_id not in contract_ids:
            continue
        try:
            results.append(probe.run(target_root))
        except Exception as exc:  # a probe bug must not kill the gate
            logger.warning("contract %s probe crashed: %s", probe.contract_id, exc)
            results.append(ContractResult(probe.contract_id, False, "probe crashed", str(exc)))
    return results


def contract_summary(results: List[ContractResult]) -> dict:
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "failed_ids": [r.contract_id for r in results if not r.passed],
    }
