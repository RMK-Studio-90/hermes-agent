"""Node J (store) unit tests — adaptive historical outcome store."""

from __future__ import annotations

import threading
import time

import pytest

from agent.routing.history import OutcomeStats, RoutingHistory


@pytest.fixture
def hist(tmp_path) -> RoutingHistory:
    h = RoutingHistory(db_path=tmp_path / "outcomes.db")
    yield h
    h.close()


def test_empty_store_returns_neutral_prior(hist: RoutingHistory) -> None:
    s = hist.stats("openrouter", "free/model", "ctx8k")
    assert s.is_prior is True
    assert s.success_rate == 0.5
    assert s.p50_latency_ms is None
    assert s.n == 0


def test_all_success_gives_high_rate_and_median_latency(hist: RoutingHistory) -> None:
    now = time.time()
    for lat in (100.0, 200.0, 300.0):
        hist.record("p", "m", "ctx8k", ok=True, latency_ms=lat, ts=now)
    s = hist.stats("p", "m", "ctx8k", now=now)
    assert s.n == 3
    assert s.success_rate == pytest.approx(1.0)
    assert s.p50_latency_ms == 200.0


def test_failures_drag_success_rate_down(hist: RoutingHistory) -> None:
    now = time.time()
    for _ in range(3):
        hist.record("p", "m", "ctx8k", ok=True, latency_ms=100.0, ts=now)
    for _ in range(1):
        hist.record("p", "m", "ctx8k", ok=False, reason="rate_limit", ts=now)
    s = hist.stats("p", "m", "ctx8k", now=now)
    assert s.n == 4
    assert s.success_rate == pytest.approx(0.75)


def test_recency_decay_weights_recent_rows_more(hist: RoutingHistory) -> None:
    now = time.time()
    # Old failures, recent successes; half-life 1h.
    for _ in range(10):
        hist.record("p", "m", "ctx8k", ok=False, reason="server_error", ts=now - 7200)
    for _ in range(10):
        hist.record("p", "m", "ctx8k", ok=True, latency_ms=50.0, ts=now - 60)
    s = hist.stats("p", "m", "ctx8k", half_life_seconds=3600, now=now)
    # 2h-old rows weigh 0.25; 1-min-old rows weigh ~1. So success dominates.
    assert s.success_rate > 0.75


def test_cap_class_scoping(hist: RoutingHistory) -> None:
    now = time.time()
    hist.record("p", "m", "ctx8k", ok=True, latency_ms=10.0, ts=now)
    hist.record("p", "m", "vision+ctx8k", ok=False, reason="model_not_found", ts=now)
    assert hist.stats("p", "m", "ctx8k", now=now).success_rate == pytest.approx(1.0)
    assert hist.stats("p", "m", "vision+ctx8k", now=now).success_rate == pytest.approx(0.0)
    # Aggregate across buckets when cap_class is None.
    agg = hist.stats("p", "m", None, now=now)
    assert agg.n == 2
    assert agg.success_rate == pytest.approx(0.5)


def test_window_caps_rows_considered(hist: RoutingHistory) -> None:
    now = time.time()
    for i in range(50):
        hist.record("p", "m", "ctx8k", ok=(i >= 10), ts=now - (50 - i))
    # newest 5 rows are all ok
    s = hist.stats("p", "m", "ctx8k", window=5, now=now)
    assert s.n == 5
    assert s.success_rate == pytest.approx(1.0)


def test_provider_normalised_on_read_and_write(hist: RoutingHistory) -> None:
    now = time.time()
    hist.record("  OpenRouter ", "m", "ctx8k", ok=True, latency_ms=1.0, ts=now)
    assert hist.stats("openrouter", "m", "ctx8k", now=now).n == 1


def test_prune_keeps_newest_per_route(hist: RoutingHistory) -> None:
    now = time.time()
    for i in range(20):
        hist.record("p", "m", "ctx8k", ok=True, ts=now - (20 - i))
    deleted = hist.prune(keep_per_route=5)
    assert deleted == 15
    assert hist.stats("p", "m", "ctx8k", window=1000, now=now).n == 5


def test_clear(hist: RoutingHistory) -> None:
    hist.record("p", "m", "ctx8k", ok=True)
    hist.clear()
    assert hist.stats("p", "m", "ctx8k").n == 0


def test_concurrent_writes_do_not_corrupt(hist: RoutingHistory) -> None:
    now = time.time()

    def worker(n: int) -> None:
        for _ in range(25):
            hist.record("p", "m", "ctx8k", ok=(n % 2 == 0), latency_ms=float(n), ts=now)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = hist.stats("p", "m", "ctx8k", window=10_000, now=now)
    assert s.n == 200


def test_default_path_under_hermes_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    h = RoutingHistory()
    h.record("p", "m", "ctx8k", ok=True)
    assert h.path is not None
    assert h.path.exists()
    assert h.path.parent.name == "routing"
    h.close()


def test_neutral_stats_is_frozen_outcomestats() -> None:
    from agent.routing.history import NEUTRAL_STATS

    assert isinstance(NEUTRAL_STATS, OutcomeStats)
    assert NEUTRAL_STATS.is_prior is True
