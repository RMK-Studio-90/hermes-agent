"""Local-first must not mean "wait for an offline local model on every request".

A loopback endpoint (LM Studio, Ollama, ...) that is not accepting connections is
skipped at candidate admission within a short bounded probe, recorded through the
existing health/TTL machinery (EXCLUDED, short TTL -> half-open re-probe), and the
next candidate in rank order carries the turn. The registry's ``available`` flag
is never rewritten: it stays a statement about the model catalogue, while
reachability is runtime health.
"""
from __future__ import annotations

import socket
import time
from types import SimpleNamespace

import pytest

from agent.models_dev import ModelCapabilities
from agent.routing import integration
from agent.routing.registry import HealthStatus, RouteRegistry


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    integration.reset_local_probe_cache()
    yield
    integration.reset_local_probe_cache()


@pytest.fixture
def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                       # nothing listens here any more -> ECONNREFUSED
    return port


@pytest.fixture
def open_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    yield s.getsockname()[1]
    s.close()


LOCAL = ("lmstudio", "qwen/qwen3-coder-30b")
CLOUD = ("omniroute", "oc/nemotron-3-ultra-free")
CLOUD_CONN = {"omniroute": {"base_url": "https://gateway.example.invalid/v1"}}


def _conns(port, **extra):
    return {"lmstudio": {"base_url": f"http://127.0.0.1:{port}/v1"}, **CLOUD_CONN, **extra}


def test_offline_loopback_provider_is_rejected_fast_and_cloud_is_admitted(closed_port):
    t0 = time.monotonic()
    admitted, rejected = integration.admit_candidate_connections(
        [LOCAL, CLOUD], {}, _conns(closed_port), "none")
    assert time.monotonic() - t0 < 1.5
    assert admitted == [CLOUD]
    assert [(r["route"], r["reason"], r["stage"]) for r in rejected] == [
        ([LOCAL[0], LOCAL[1]], integration.LOCAL_ENDPOINT_UNREACHABLE, "local_availability")]


def test_online_loopback_provider_is_admitted_in_rank_order(open_port):
    admitted, rejected = integration.admit_candidate_connections(
        [LOCAL, CLOUD], {}, _conns(open_port), "none")
    assert admitted == [LOCAL, CLOUD]
    assert rejected == []


def test_same_provider_reselect_is_also_probed(closed_port):
    """The agent already being on lmstudio does not exempt a dead endpoint."""
    admitted, rejected = integration.admit_candidate_connections(
        [LOCAL, CLOUD], {}, _conns(closed_port), "lmstudio")
    assert admitted == [CLOUD]
    assert rejected and rejected[0]["reason"] == integration.LOCAL_ENDPOINT_UNREACHABLE


def test_first_class_provider_without_config_probes_its_default_endpoint(monkeypatch):
    """No ``providers:`` entry: the canonical LM Studio endpoint (or LM_BASE_URL) is probed."""
    seen = []
    monkeypatch.setattr(integration, "_tcp_reachable",
                        lambda host, port, timeout: seen.append((host, port)) or False)
    monkeypatch.delenv("LM_BASE_URL", raising=False)
    admitted, rejected = integration.admit_candidate_connections([LOCAL, CLOUD], {}, CLOUD_CONN, "none")
    assert seen == [("127.0.0.1", 1234)]
    assert admitted == [CLOUD]
    assert rejected[0]["reason"] == integration.LOCAL_ENDPOINT_UNREACHABLE


def test_remote_endpoints_are_never_probed(monkeypatch):
    monkeypatch.setattr(integration, "_tcp_reachable",
                        lambda *a, **k: pytest.fail("a non-loopback endpoint must not be probed"))
    admitted, rejected = integration.admit_candidate_connections([CLOUD], {}, CLOUD_CONN, "none")
    assert admitted == [CLOUD] and rejected == []


def test_probe_result_is_cached_so_there_is_no_repeated_local_timeout_loop(monkeypatch, closed_port):
    calls = []
    real = integration._tcp_reachable
    monkeypatch.setattr(integration, "_tcp_reachable",
                        lambda host, port, timeout: calls.append(1) or real(host, port, timeout))
    for _ in range(5):
        integration.admit_candidate_connections([LOCAL, LOCAL], {}, _conns(closed_port), "none")
    assert len(calls) == 1


def test_recovered_local_server_is_readmitted_after_the_cache_lapses(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    clock = [1000.0]
    monkeypatch.setattr(integration, "_probe_clock", lambda: clock[0])
    assert integration.admit_candidate_connections([LOCAL], {}, _conns(port), "none")[0] == []

    srv = socket.socket()
    srv.bind(("127.0.0.1", port))
    srv.listen(4)
    try:
        assert integration.admit_candidate_connections([LOCAL], {}, _conns(port), "none")[0] == []
        clock[0] += integration.LOCAL_PROBE_DOWN_TTL_SECONDS + 1
        assert integration.admit_candidate_connections([LOCAL], {}, _conns(port), "none")[0] == [LOCAL]
    finally:
        srv.close()


def test_rejection_is_recorded_as_short_ttl_health_not_as_availability():
    reg = RouteRegistry()
    entry = reg.register(LOCAL[0], LOCAL[1], capabilities=ModelCapabilities(
        supports_tools=True, context_window=128_000, max_output_tokens=4096),
        billing_class="ZERO_ADDITIONAL_COST")
    assert entry.available is True
    integration._note_connection_rejection(
        SimpleNamespace(), LOCAL, registry=reg, reason=integration.LOCAL_ENDPOINT_UNREACHABLE)
    assert entry.available is True, "availability metadata must not be falsified"
    assert entry.effective_status() == HealthStatus.EXCLUDED
    assert entry.is_available() is False
    assert entry.health_reason == "local_endpoint_unreachable"
    assert entry.health_ttl.total_seconds() <= 60, "an offline local server must be re-probed soon"
    entry.health_since -= entry.health_ttl              # TTL lapses -> half-open re-admission
    reg.resolve_expiries()
    assert entry.is_available() is True
