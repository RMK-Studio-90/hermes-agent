"""Baseline-Differential tests (RED-first).

A certified state stores its test PASS/FAIL/SKIP fingerprint; the next update
compares against it so KNOWN_BASELINE_FAILURE (e.g. memory_scope: 11 FAIL) is
never mistaken for a new REGRESSION.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

sys_path = str(Path(__file__).resolve().parents[2])


def _mod():
    import importlib
    import sys
    sys.path.insert(0, sys_path)
    return importlib.import_module("hermes_cli.update_safe_baseline")


def test_classify_identical_baseline_is_known_baseline(tmp_path):
    b = _mod()
    old = {"memory_scope": (0, 11, 0), "core": (100, 0, 0)}
    new = {"memory_scope": (0, 11, 0), "core": (100, 0, 0)}
    verdict = b.diff_baseline(old, new)
    assert verdict["verdict"] == "KNOWN_BASELINE_FAILURE"
    assert verdict["new_regressions"] == []


def test_classify_new_failure_is_regression(tmp_path):
    b = _mod()
    old = {"memory_scope": (0, 11, 0), "core": (100, 0, 0)}
    new = {"memory_scope": (0, 12, 0), "core": (100, 0, 0)}   # 11 -> 12 FAIL
    verdict = b.diff_baseline(old, new)
    assert verdict["verdict"] == "REGRESSION"
    assert "memory_scope" in verdict["new_regressions"]


def test_save_and_load_roundtrip(tmp_path):
    b = _mod()
    path = tmp_path / "baseline.json"
    payload = {"head": "abc123", "suites": {"memory_scope": [0, 11, 0]}}
    b.save_baseline(path, payload)
    loaded = b.load_baseline(path)
    assert loaded == payload
