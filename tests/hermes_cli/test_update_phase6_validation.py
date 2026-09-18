"""Unit tests for the Phase 6 post-update validation / rollback helpers.

Covers the decision-critical pieces of the Phase 6 gate in
``hermes_cli/update_cmd_maint.py``:

* ``_run_post_update_validation`` — the offline routing probe GATES; the doctor
  report is report-only and must never veto an update.
* ``_rollback_after_failed_validation`` — only a VERIFIED pre-update SafeState
  may be restored; restore is ``state_mode="live"`` + ``code_mode="live-git"``
  (forward-apply on the scratched HEAD); the function always returns False, so
  a failed validation is never accepted.
* ``_print_update_summary`` — ``validation_ok=False`` withholds the "✓ Update
  complete!" banner and returns False.
* ``_routing_probe_failures`` — exercised against synthetic first-party routing
  modules in a real subprocess (hermetic, offline).
* ``_run_quick_snapshots`` — the pre-update checkpoint is a SafeState, with a
  guaranteed fallback to the legacy state-only snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.update_cmd_maint import (
    _print_update_summary,
    _rollback_after_failed_validation,
    _routing_probe_failures,
    _run_doctor_probe,
    _run_post_update_validation,
    _run_quick_snapshots,
)

_FAILD_VALIDATION = {
    "ok": False,
    "checks": [
        {
            "name": "routing_probe",
            "ok": False,
            "report_only": False,
            "detail": "agent.routing.router -> ImportError: boom",
        }
    ],
    "pre_update_snapshot_id": "snap-1",
}


def _synthetic_routing_root(tmp_path: Path, registry: dict) -> tuple[Path, Path]:
    """A hermetic root whose first-party routing chain is three empty modules,
    plus a home carrying a real ``routing/registry.json``."""
    root = tmp_path / "root"
    pkg = root / "agent" / "routing"
    pkg.mkdir(parents=True)
    (root / "agent" / "__init__.py").write_text("", encoding="utf-8")
    (root / "agent" / "routing" / "__init__.py").write_text("", encoding="utf-8")
    for name in ("registry", "router", "logical"):
        (pkg / f"{name}.py").write_text("", encoding="utf-8")
    home = tmp_path / "home"
    (home / "routing").mkdir(parents=True)
    (home / "routing" / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    return root, home


# ---------------------------------------------------------------------------
# _routing_probe_failures — real subprocess, hermetic synthetic tree
# ---------------------------------------------------------------------------

def test_routing_probe_clean_registry_no_failures(tmp_path):
    root, home = _synthetic_routing_root(tmp_path, {"models": []})
    assert _routing_probe_failures(root, home) == {}


def test_routing_probe_missing_models_key_is_registry_invalid(tmp_path):
    root, home = _synthetic_routing_root(tmp_path, {"routes": [{"model": "x"}]})
    failures = _routing_probe_failures(root, home)
    assert "routing/registry.json" in failures
    kind, _ = failures["routing/registry.json"]
    assert kind == "RegistryInvalid"


def test_routing_probe_unreadable_registry_is_registry_error(tmp_path):
    root, home = _synthetic_routing_root(tmp_path, {})  # overwritten below
    (home / "routing" / "registry.json").write_text("not json {{{", encoding="utf-8")
    failures = _routing_probe_failures(root, home)
    assert "routing/registry.json" in failures
    assert failures["routing/registry.json"][0] == "RegistryUnreadable"


# ---------------------------------------------------------------------------
# _run_post_update_validation — routing gates, doctor is report-only
# ---------------------------------------------------------------------------

def test_validation_routing_failure_gates_and_skips_doctor(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_cmd_maint._routing_probe_failures",
        lambda root, home: {"agent.routing.router": ("ImportError", "boom")},
    )
    doctor_calls: list = []
    monkeypatch.setattr(
        "hermes_cli.update_cmd_maint._run_doctor_probe",
        lambda root: doctor_calls.append(root) or {"status": "ok"},
    )
    validation = _run_post_update_validation("snap-1", root="/r", home="/h")
    assert validation["ok"] is False
    assert validation["pre_update_snapshot_id"] == "snap-1"
    by_name = {c["name"]: c for c in validation["checks"]}
    assert by_name["routing_probe"]["ok"] is False
    assert by_name["routing_probe"]["report_only"] is False
    assert "doctor_probe" not in by_name
    assert doctor_calls == []


def test_validation_clean_routing_runs_doctor_but_doctor_cannot_veto(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_cmd_maint._routing_probe_failures",
        lambda root, home: {},
    )
    # A failing doctor (report-only) must NOT fail the gate.
    monkeypatch.setattr(
        "hermes_cli.update_cmd_maint._run_doctor_probe",
        lambda root: {"status": "failed", "detail": "network down"},
    )
    validation = _run_post_update_validation("snap-1", root="/r", home="/h")
    assert validation["ok"] is True
    by_name = {c["name"]: c for c in validation["checks"]}
    assert by_name["routing_probe"]["ok"] is True
    assert by_name["doctor_probe"]["report_only"] is True
    assert by_name["doctor_probe"]["detail"] == "network down"


# ---------------------------------------------------------------------------
# _rollback_after_failed_validation — verify first, live-git restore, never accepted
# ---------------------------------------------------------------------------

def test_rollback_without_snapshot_prints_manual_recovery(monkeypatch, capsys, tmp_path):
    root = tmp_path / "code"
    home = tmp_path / "home"
    verify_calls: list = []
    monkeypatch.setattr(
        "hermes_cli.rmk_safestate.verify_safestate",
        lambda *a, **k: verify_calls.append(1) or {},
    )
    ok = _rollback_after_failed_validation(None, _FAILD_VALIDATION, root=root, home=home)
    assert ok is False
    assert "cannot roll back automatically" in capsys.readouterr().out
    assert not verify_calls


def test_rollback_refuses_unverified_safestate(monkeypatch, capsys, tmp_path):
    # Isolation hardening: use real tmp dirs for root/home. If a mid-suite
    # updater path evicts hermes_cli.rmk_safestate from sys.modules, a later
    # re-import creates a divergent module object and this monkeypatch may not
    # apply at call time; the real verify_safestate then runs. Against real
    # empty dirs it returns success=False GRACEFULLY (missing snapshot) and the
    # test still asserts the exact contract — never restore an unverified snap.
    # With the string paths "/r"/"/h" the leaked real verify crash-typed instead.
    root = tmp_path / "code"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    restore_calls: list = []
    monkeypatch.setattr(
        "hermes_cli.rmk_safestate.verify_safestate",
        lambda snap_id, **kw: {
            "success": False,
            "checks": [{"name": "code_patch", "ok": False}],
            "error": None,
        },
    )
    monkeypatch.setattr(
        "hermes_cli.rmk_safestate.restore_safestate",
        lambda *a, **k: restore_calls.append(1) or {},
    )
    ok = _rollback_after_failed_validation("snap-1", _FAILD_VALIDATION, root=root, home=home)
    assert ok is False
    out = capsys.readouterr().out
    assert "did NOT verify" in out
    assert "refusing to restore" in out
    assert not restore_calls


def test_rollback_restores_verified_safestate_live(monkeypatch, capsys, tmp_path):
    # Real tmp dirs (isolation hardening — see test_rollback_refuses...): the
    # kwargs assertions below stay exact under the healthy (patched) path.
    root = tmp_path / "code"
    home = tmp_path / "home"
    root.mkdir()
    home.mkdir()
    verify_kw: dict = {}
    restore_kw: dict = {}

    def fake_verify(snap_id, **kw):
        verify_kw.update({"snap_id": snap_id, **kw})
        return {"success": True, "checks": []}

    def fake_restore(snap_id, **kw):
        restore_kw.update({"snap_id": snap_id, **kw})
        return {
            "code_result": {"mode": "live-git", "success": True, "detail": "reset to abc"},
            "state_result": {"mode": "live", "success": True},
        }

    monkeypatch.setattr("hermes_cli.rmk_safestate.verify_safestate", fake_verify)
    monkeypatch.setattr("hermes_cli.rmk_safestate.restore_safestate", fake_restore)
    ok = _rollback_after_failed_validation("snap-1", _FAILD_VALIDATION, root=root, home=home)
    assert ok is False  # the update is never accepted
    assert verify_kw == {
        "snap_id": "snap-1",
        "hermes_home": home,
        "code_root": root,
        "include_routing_check": False,
    }
    assert restore_kw == {
        "snap_id": "snap-1",
        "hermes_home": home,
        "code_root": root,
        "state_mode": "live",
        "code_mode": "live-git",
    }
    out = capsys.readouterr().out
    assert "verified — restoring" in out
    assert "Code rolled back" in out


# ---------------------------------------------------------------------------
# _print_update_summary — validation_ok withholds the completion banner
# ---------------------------------------------------------------------------

def test_summary_validation_failure_withholds_completion(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.update_cmd._post_update_sqlite_runtime_status", lambda: (True, None)
    )
    ok = _print_update_summary(
        node_failures=[], desktop_build_ok=True, pre_update_version="v1", validation_ok=False
    )
    assert ok is False
    out = capsys.readouterr().out
    assert "Update partially complete" in out
    assert "post-update validation" in out
    assert "Update complete!" not in out


def test_summary_all_ok_returns_true(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.update_cmd._post_update_sqlite_runtime_status", lambda: (True, None)
    )
    completion: list[str] = []
    monkeypatch.setattr(
        "hermes_cli.update_cmd._update_complete_message", lambda v: f"completion {v}"
    )
    monkeypatch.setattr(
        "hermes_cli.update_cmd_maint._print_update_completion", completion.append
    )
    ok = _print_update_summary(
        node_failures=[], desktop_build_ok=True, pre_update_version="v1", validation_ok=True
    )
    assert ok is True
    assert completion == ["completion v1"]


def test_summary_node_failures_print_partial_banner_but_do_not_demote(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.update_cmd._post_update_sqlite_runtime_status", lambda: (True, None)
    )
    # Node failures are banner-only: the returned verdict is
    # ``desktop_build_ok and sqlite_runtime_ok and validation_ok`` — never demoted
    # by node_failures (see update_cmd_maint._print_update_summary).
    ok = _print_update_summary(
        node_failures=["dashboard"], desktop_build_ok=True, pre_update_version="v1",
    )
    assert ok is True
    out = capsys.readouterr().out
    assert "Update partially complete" in out
    assert "dashboard" in out


def test_summary_desktop_failure_demotes(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.update_cmd._post_update_sqlite_runtime_status", lambda: (True, None)
    )
    ok = _print_update_summary(
        node_failures=[], desktop_build_ok=False, pre_update_version="v1",
    )
    assert ok is False
    assert "desktop app was not rebuilt" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _run_quick_snapshots — SafeState checkpoint with legacy fallback
# ---------------------------------------------------------------------------

def test_quick_snapshots_uses_safestate_not_legacy(monkeypatch, capsys):
    captured: dict = {}
    monkeypatch.setattr(
        "hermes_cli.rmk_safestate.create_safestate",
        lambda **kw: captured.update(kw) or {"snap_id": "phase6-test-1", "success": True},
    )
    legacy_calls: list = []
    monkeypatch.setattr(
        "hermes_cli.backup.create_quick_snapshot",
        lambda **kw: legacy_calls.append(1) or "legacy-1",
    )
    monkeypatch.setattr(
        "hermes_cli.backup.create_pre_update_snapshots_all_profiles", lambda **kw: {}
    )
    monkeypatch.setattr(
        "hermes_cli.update_cmd_maint._verify_state_db_after_snapshot", lambda snap_id: None
    )
    snapshot_id = _run_quick_snapshots()
    assert snapshot_id == "phase6-test-1"
    assert captured.get("label") == "pre-update"
    assert captured.get("keep") is not None and captured.get("max_file_size") is not None
    assert not legacy_calls
    assert "Pre-update SafeState" in capsys.readouterr().out


def test_quick_snapshots_falls_back_to_legacy(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.rmk_safestate.create_safestate",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("rmk layer down")),
    )
    monkeypatch.setattr(
        "hermes_cli.backup.create_quick_snapshot",
        lambda **kw: "legacy-1",
    )
    monkeypatch.setattr(
        "hermes_cli.backup.create_pre_update_snapshots_all_profiles", lambda **kw: {}
    )
    monkeypatch.setattr(
        "hermes_cli.update_cmd_maint._verify_state_db_after_snapshot", lambda snap_id: None
    )
    snapshot_id = _run_quick_snapshots()
    assert snapshot_id == "legacy-1"  # the checkpoint is never lost
    assert "Pre-update snapshot" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _run_doctor_probe — generated script must compile and quote the skipped names
# ---------------------------------------------------------------------------

def test_doctor_probe_generated_script_compiles_and_quotes_skips(monkeypatch):
    """The subprocess script generated by _run_doctor_probe must be valid Python, AND the
    skipped-check names must be substituted as QUOTED strings. This pins the regression where
    ``(%s,)`` was fed ``", ".join(_DOCTOR_SKIPPED_CHECKS)`` (bare identifiers) — the child died
    with ``NameError: name '_check_npm_audit' is not defined`` and the update never got its
    report-only doctor row."""
    import types as _types

    captured: dict = {}

    def fake_run(args, **kw):
        captured["probe"] = args[-1]
        return _types.SimpleNamespace(stdout="", returncode=1, stderr="")

    monkeypatch.setattr("hermes_cli.update_cmd_maint.subprocess.run", fake_run)
    # fake_run's empty stdout can never contain the marker, so the report is 'failed' —
    # irrelevant here; we only need the generated script.
    result = _run_doctor_probe(root=Path(__file__).resolve().parents[2])
    assert result["status"] == "failed"

    probe = captured["probe"]
    compile(probe, "<doctor-probe>", "exec")  # raises AssertionError/err if invalid

    # The SKIP filter references real identifiers (never bare).
    assert "if c.__name__ not in (" in probe
    assert "'_check_npm_audit'" in probe
    assert "'_check_api_connectivity'" in probe
    assert "(_check_npm_audit" not in probe
    assert "(_check_api_connectivity" not in probe