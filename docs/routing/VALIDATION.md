# Node L — Runtime Validation

Date: 2026-09-09. Python 3.11.15 (`venv/Scripts/python.exe`, the repo's dev venv).

## L.1 Demo scenarios

```
$ python -m agent.routing.demo --scenario recover
[recover] initial pick: ('free', 'alpha')
[recover] injected rate_limit on free/alpha -> health=excluded ttl=300s
[recover] recovery pick: ('free', 'bravo')  hop=1
[recover] after TTL: free/alpha health=healthy available=True half_open=True
[recover] post-recovery pick: ('free', 'alpha')

--- telemetry (explain) ---
… select   free/alpha   mode=auto cap=tools+reasoning+ctx8k
… health   free/alpha   rate_limit -> exclude=excluded ttl=300 n=1
… recovery free/bravo   mode=recovery cap=tools+reasoning+ctx8k trigger=rate_limit hop=1
… select   free/alpha   mode=auto cap=tools+reasoning+ctx8k

[recover] 5/5 checks passed -> PASS   (exit 0)

$ python -m agent.routing.demo --scenario select    -> [select] PASS   (exit 0)
$ python -m agent.routing.demo --scenario exhaust   -> [exhaust] PASS  (exit 0)
```

**Runtime recovery demonstrated:** an injected `rate_limit` on the primary free
model auto-routes to a *healthy free* model (never the paid one — Free-First held
under failure); the decision, the health change and the recovery hop are all in
telemetry; after the health TTL lapses the primary is re-admitted automatically
(half-open) and the next selection returns to it.

## L.2 Test suites (canonical per-file-isolated runner)

```
$ bash scripts/run_tests.sh tests/agent/routing/ tests/integration/test_routing_end_to_end.py \
        tests/agent/test_auxiliary_main_first.py tests/agent/test_failover_identity.py
=== Summary: 12 files, 148 tests passed, 0 failed (100% complete) ===
```

Routing unit suite (`tests/agent/routing/`): **114** — registry 15, capabilities 9,
health 18, history 12, scoring 16, override 13, router 14, telemetry 9, integration 8.
End-to-end (`tests/integration/test_routing_end_to_end.py`): **6**.

## L.3 Flag-off regression (byte-identical existing behaviour)

`routing.adaptive.enabled` absent/false is the default. With the flag off:

* `tests/agent/test_failover_identity.py` — 11 passed
* `tests/gateway/test_fallback_chain_reload.py`, `tests/gateway/test_empty_model_recovery.py`,
  `tests/agent/test_compression_stall_fallback_78981.py`, `tests/agent/test_credential_pool_routing.py`
  — 46 passed together
* `tests/agent/test_auxiliary_client.py`, `tests/agent/test_auxiliary_main_first.py`,
  `tests/agent/test_chat_completion_helpers_provider_sort.py` — pass under the isolated runner

The single live-path edit (`agent/chat_completion_helpers.try_activate_fallback`) is a
guarded `try: reorder_fallback_chain(agent, reason) except Exception: pass`; with the flag
off `reorder_fallback_chain` returns `None` before touching anything.

## L.4 Rollback

* Flip `routing.adaptive.enabled` to false (or unset) — instant, no deploy.
* Full removal: delete `agent/routing/`, `tests/agent/routing/`,
  `tests/integration/test_routing_end_to_end.py`; revert the hunk in
  `agent/chat_completion_helpers.py`; restore `agent/model_router.py` /
  `agent/canonical_registry.py` (or delete, once their only importer —
  `agent/agent_init.py:32`, name-only — is dropped).
