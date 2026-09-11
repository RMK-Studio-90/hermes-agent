from agent.routing.history import RoutingHistory
from agent.routing import telemetry
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def test_history_and_events_follow_profile_context(tmp_path):
    history = RoutingHistory()
    telemetry.configure(None, reset_buffer=True)
    token = set_hermes_home_override(tmp_path / "a")
    try:
        history.record("test", "model", "", True)
        telemetry._emit(telemetry.RoutingDecisionRecord(ts="test", event="outcome"))
        second = set_hermes_home_override(tmp_path / "b")
        try:
            assert history.stats("test", "model").n == 0
            assert telemetry.recent() == []
            telemetry._emit(telemetry.RoutingDecisionRecord(ts="test-b", event="outcome"))
        finally:
            reset_hermes_home_override(second)
        assert history.stats("test", "model").n == 1
        assert [r.ts for r in telemetry.recent()] == ["test"]
        assert (tmp_path / "a/routing/decisions.jsonl").exists()
        assert (tmp_path / "b/routing/decisions.jsonl").exists()
    finally:
        history.close()
        telemetry.configure(None, reset_buffer=True)
        reset_hermes_home_override(token)
