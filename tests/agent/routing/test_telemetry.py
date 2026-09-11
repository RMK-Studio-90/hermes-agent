"""Node I unit tests — routing telemetry / explainability."""

from __future__ import annotations

import json

import pytest

from agent.routing import telemetry
from agent.routing.health import HealthDecision
from agent.routing.router import Decision


@pytest.fixture(autouse=True)
def isolated_sink(tmp_path):
    telemetry.configure(jsonl_path=tmp_path / "decisions.jsonl", reset_buffer=True)
    yield tmp_path / "decisions.jsonl"
    telemetry.configure(None, reset_buffer=True)


def _decision(**kw) -> Decision:
    base = dict(
        provider="free", model="primary", mode="auto",
        why={"mode": "auto", "cap_class": "tools+ctx8k",
             "ranked": [{"route": ["free", "primary"],
                         "breakdown": {"cost_tier": "free", "health": "healthy",
                                       "success_rate": 0.9, "history_rows": 12,
                                       "p50_latency_ms": 120.0}}],
             "rejected": [{"route": ["free", "bad"], "reason": "unavailable:excluded"}]},
        requires_paid=False,
    )
    base.update(kw)
    return Decision(**base)


def test_record_decision_buffers_and_writes_jsonl(isolated_sink) -> None:
    rec = telemetry.record_decision(_decision())
    assert rec.event == "select"
    assert rec.chosen == ["free", "primary"]
    assert rec.cap_class == "tools+ctx8k"

    buf = telemetry.recent(10)
    assert len(buf) == 1

    lines = isolated_sink.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["chosen"] == ["free", "primary"]
    assert row["event"] == "select"


def test_recovery_decision_marked_recovery() -> None:
    d = _decision(mode="recovery", recovery_hops=2,
                  why={"mode": "recovery", "trigger": "rate_limit", "cap_class": "ctx8k",
                       "ranked": [], "rejected": []})
    rec = telemetry.record_decision(d)
    assert rec.event == "recovery"
    assert rec.trigger == "rate_limit"
    assert rec.recovery_hops == 2


def test_requires_paid_and_exhausted_flags() -> None:
    d = _decision(provider=None, model=None, requires_paid=True, exhausted=True,
                  why={"mode": "auto", "cap_class": "ctx8k", "ranked": [], "rejected": []})
    rec = telemetry.record_decision(d)
    assert rec.requires_paid is True
    assert rec.exhausted is True
    assert rec.chosen is None


def test_override_fields_extracted() -> None:
    d = _decision(mode="override",
                  why={"override": {"source": "turn", "mode": "strict", "forced": True},
                       "cap_class": "ctx8k"})
    rec = telemetry.record_decision(d)
    assert rec.override_source == "turn"
    assert rec.override_mode == "strict"


def test_record_health() -> None:
    hd = HealthDecision(provider="free", model="primary", reason="rate_limit",
                        action="exclude", new_status="excluded", ttl_seconds=300,
                        consecutive_failures=1)
    rec = telemetry.record_health(hd)
    assert rec.event == "health"
    assert rec.health["action"] == "exclude"
    assert rec.trigger == "rate_limit"


def test_explain_renders_without_error() -> None:
    telemetry.record_decision(_decision())
    telemetry.record_health(HealthDecision(provider="free", model="bad", reason="server_error",
                                           action="degrade", new_status="degraded",
                                           ttl_seconds=120, consecutive_failures=2))
    text = telemetry.explain(10)
    assert "free/primary" in text
    assert "server_error" in text
    assert "tier=free" in text


def test_explain_empty_buffer() -> None:
    assert "no routing decisions" in telemetry.explain(10)


def test_sink_failure_never_raises(tmp_path, monkeypatch) -> None:
    telemetry.configure(jsonl_path=tmp_path / "nonexist" / "x" / "d.jsonl", reset_buffer=True)

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.open", boom)
    rec = telemetry.record_decision(_decision())  # must not raise
    assert rec is not None
    assert len(telemetry.recent(5)) == 1


def test_record_to_json_roundtrips() -> None:
    rec = telemetry.record_decision(_decision())
    parsed = json.loads(rec.to_json())
    assert parsed["cap_class"] == "tools+ctx8k"
