"""GRAPH 5A — REAL INVOKER (shadow-mode oneshot driver).

BUILD-standard additive module (K09 §15 shadow gate). Executes one K06 golden-set
case arm as a REAL Hermes agent run via the headless oneshot path in an ISOLATED
sandbox, so the shadow A/B can compare baseline vs Operating-Brain candidate on
real model behavior without touching productive state.

Contract: `invoker(case_id, arm, input_text, case) -> dict` consumed by
experiments/ab_runner.py invoke_arm().

Invariants (same as BUILD-05 I1..I4, enforced + tested):
  I1  input_text is passed byte-identical to the subprocess (no rewrite).
  I2  infra failures (timeout, provider, partial/no-response) map to
      TIMEOUT / PROVIDER_ERROR / RATE_LIMITED — NEVER a quality FAIL; the
      pair becomes eval_state='inconclusive' and cannot poison PHPR.
  I3  reproducible provenance: execution_id, UTC timestamps, model/provider/
      config_version, usage-file sha.
  I4  NO promotion, NO activation, NO feature-flag write, NO productive
      mutation: only the sandbox home/board (HERMES_HOME / HERMES_KANBAN_HOME /
      HERMES_KANBAN_DB) is written, never the live board or live config.

Fields that are not observable through the oneshot usage channel
(tool_call_count, tool_call_order_hash, clarification_count) are reported as
None with field_unavailable=True — never fabricated as 0.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

CONFIG_VERSION = 40  # canonical K05 §6.6 value (matches BUILD-05 mock/default)

# Infra statuses per BUILD-05 taxonomy (I2) — never quality FAIL.
_INFRA = {"PROVIDER_ERROR", "RATE_LIMITED", "TIMEOUT"}


class InvokerError(Exception):
    """Infrastructure-class invocation failure (I2)."""


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sandbox_env(
    *,
    home: Optional[str] = None,
    kanban_home: Optional[str] = None,
    kanban_db: Optional[str] = None,
) -> dict:
    """Smallest env delta for an isolated shadow run.

    Points HERMES_HOME / HERMES_KANBAN_HOME / HERMES_KANBAN_DB at the sandbox
    and REMOVES worker/child-context variables so the run can never mutate the
    live board or behave like a claimed kanban worker.
    """
    env = os.environ.copy()
    # Never inherit worker/child semantics into the root invoker process.
    for k in (
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_DELEGATED_CHILD_CONTEXT",
    ):
        env.pop(k, None)
    if home:
        env["HERMES_HOME"] = home
    if kanban_home:
        env["HERMES_KANBAN_HOME"] = kanban_home
    if kanban_db:
        env["HERMES_KANBAN_DB"] = kanban_db
    return env


def make_invoker(
    *,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    home: Optional[str] = None,
    kanban_home: Optional[str] = None,
    kanban_db: Optional[str] = None,
    timeout_s: float = 600.0,
    python: Optional[str] = None,
    usage_dir: Optional[str] = None,
) -> Callable:
    """Build a BUILD-05-compatible real invoker.

    Each invocation spawns `python -m hermes_cli.main -z <input_text>` with an
    isolated sandbox env and a per-run usage file; the final response text is
    captured from stdout, the usage JSON supplies cost/token/model telemetry.
    """
    if model and not provider:
        raise InvokerError("model without provider is ambiguous (oneshot rule)")
    py = python or os.environ.get("PYTHON") or "python"
    usage_root = Path(usage_dir) if usage_dir else None
    if usage_root is not None:
        usage_root.mkdir(parents=True, exist_ok=True)

    def invoker(case_id: str, arm: str, input_text: str, case: Optional[dict] = None, **kw: Any) -> dict:
        started = time.time()
        run_id = f"exec_{case_id}_{arm}_{uuid.uuid4().hex[:12]}"
        usage_file = None
        if usage_root is not None:
            usage_file = usage_root / f"{run_id}.usage.json"
        cmd = [py, "-m", "hermes_cli.main", "-z", input_text]
        if model:
            cmd += ["-m", model, "--provider", provider]
        if usage_file is not None:
            cmd += ["--usage-file", str(usage_file)]
        env = sandbox_env(home=home, kanban_home=kanban_home, kanban_db=kanban_db)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return _infra_result(
                "TIMEOUT", run_id, case_id, arm, started,
                error=f"TimeoutExpired after {timeout_s}s",
                partial_out=(exc.stdout or "")[-400:] if exc.stdout else None,
            )
        except OSError as exc:
            return _infra_result(
                "PROVIDER_ERROR", run_id, case_id, arm, started,
                error=f"OSError: {exc}",
            )
        elapsed_ms = int((time.time() - started) * 1000)

        usage: dict = {}
        if usage_file is not None and usage_file.exists():
            try:
                usage = json.loads(usage_file.read_text(encoding="utf-8"))
            except Exception as exc:  # pragma: no cover - malformed usage file
                return _infra_result(
                    "PROVIDER_ERROR", run_id, case_id, arm, started,
                    error=f"usage file unreadable: {exc}", latency_ms=elapsed_ms,
                )
        output = proc.stdout or ""
        completed = bool(usage.get("completed", proc.returncode == 0))
        failed = bool(usage.get("failed", False))
        partial = bool(usage.get("partial", False))

        if proc.returncode != 0 or failed or not output.strip() or not completed:
            # Oneshot non-success is infra-class unless it is an explicit
            # quality/security signal, which oneshot does not expose. I2:
            # NEVER reinterpret as quality FAIL.
            if proc.returncode == 2 or partial:
                status = "TIMEOUT" if partial and not output.strip() else "PROVIDER_ERROR"
            else:
                status = "PROVIDER_ERROR"
            return _infra_result(
                status, run_id, case_id, arm, started,
                error=(proc.stderr or usage.get("error") or "")[-500:],
                latency_ms=elapsed_ms, usage=usage, output=output,
            )

        return {
            "status": "SUCCESS",
            "succeeded": True,
            "output": output,
            "model": usage.get("model") or model,
            "provider": usage.get("provider") or provider,
            "config_version": CONFIG_VERSION,
            "execution_id": run_id,
            "hard_validator_results": {},
            "cost": {
                "state": "zero" if float(usage.get("estimated_cost_usd") or 0.0) <= 0 else "unknown",
                "usd": float(usage.get("estimated_cost_usd") or 0.0),
                "tokens": {"in": int(usage.get("input_tokens") or 0),
                           "out": int(usage.get("output_tokens") or 0)},
            },
            "latency_ms": elapsed_ms,
            "started_at": _utcnow_iso(),
            "completed_at": _utcnow_iso(),
            "tool_call_count": None,
            "tool_call_order_hash": None,
            "clarification_count": None,
            "field_unavailable": ["tool_call_count", "tool_call_order_hash",
                                  "clarification_count"],
            "api_calls": int(usage.get("api_calls") or 1),
            "session_id": usage.get("session_id"),
            "input_hash": _sha256_text(input_text),
            "usage_file": str(usage_file) if usage_file else None,
        }

    return invoker


def _infra_result(
    status: str,
    run_id: str,
    case_id: str,
    arm: str,
    started: float,
    *,
    error: str,
    latency_ms: Optional[int] = None,
    usage: Optional[dict] = None,
    output: Optional[str] = None,
    partial_out: Optional[str] = None,
) -> dict:
    assert status in _INFRA, status
    return {
        "status": status,
        "succeeded": False,
        "infra_failure": True,
        "output": output or partial_out,
        "model": (usage or {}).get("model"),
        "provider": (usage or {}).get("provider"),
        "config_version": CONFIG_VERSION,
        "execution_id": run_id,
        "hard_validator_results": {},
        "cost": {"state": "zero", "usd": 0.0, "tokens": {"in": 0, "out": 0}},
        "latency_ms": latency_ms if latency_ms is not None else int((time.time() - started) * 1000),
        "error": error[:1000],
    }


if __name__ == "__main__":  # pragma: no cover
    import sys

    print("real_invoker: build module, not a CLI. Use shadow_run.py.")
    sys.exit(0)
