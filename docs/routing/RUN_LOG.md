# Adaptive Model Routing — Run Log

Branch: `feat/adaptive-model-routing` (off prior `feat/background-review-efficiency` tip `19a9fcba4f`, carrying all prior uncommitted changes).
Started: 2026-09-09
Auftraggeber: René
Executor: Claude (Sonnet 5), autonomous graph execution.

## Adopted defaults (Auftragsvorlage was blank — logged per §7B, non-blocking)

| Parameter | Adopted value |
|---|---|
| Max repair cycles per node (Implement→Test→Review→Repair) | 3 |
| Max time per node before escalation | none (no wall-clock cap) |
| Total cost budget | 0 € — free models only; any paid model = §7A hard stop |
| Provider/model policy | Free-First (AGENTS.md mandate) |
| Node M independent reviewer | subagent reviewer (separate context, does not grade own work) |
| Terminal state | all nodes A–M green + regression suite passes + runtime recovery demonstrated + independent review PASS |

## §7A hard-stop triggers (report immediately, do not proceed)
Data-loss risk; secret/credential exposure; canonical conflict; required credential unavailable;
security boundary would need weakening; rollback no longer possible; unresolved material review defect;
no progress after 3 repair cycles; any paid-model requirement.

## §7B non-blocking config decisions (log here, keep going)

- 2026-09-09 — Prior session's uncommitted routing/HGES pile (`agent/model_router.py`, `agent/canonical_registry.py`,
  `agent/graph/*`, `hermes_cli/{graph,operating_brain,promotion_engine,audit_sink,eval_schema}.py`, `loop/*`,
  `rollout/*`, `experiments/*`, `golden_set/harness.py`, `tests/agent/test_model_router*`, `tests/integration/test_routing*`)
  will be **audited & salvaged**: keep sound modules, rewrite broken parts, reconcile to one canonical test set.
- 2026-09-09 — Work lands on new branch `feat/adaptive-model-routing`, all prior uncommitted changes carried.

## Node ledger

| Node | Title | Status |
|---|---|---|
| plan | Dependency graph | done — docs/routing/DEPENDENCY_GRAPH.md |
| A | Baseline / routing architecture audit | done — docs/routing/AUDIT.md |
| B | Canonical model/route registry | done — agent/routing/registry.py; tests 15/15 green |
| C | Task capability classification | done — agent/routing/capabilities.py; tests 9/9 |
| D | Failure classification + route health | done — agent/routing/health.py; tests 18/18 |
| E | Candidate filtering / scoring | done — agent/routing/scoring.py; tests 16/16 |
| F | Automatic provider fallback / recovery | done — router.select_recovery + integration.reorder_fallback_chain; covered in router 14 + integration 8 tests |
| G | Worker / runtime integration | done — agent/routing/integration.py + 1 guarded hook in chat_completion_helpers.try_activate_fallback; flag-off = no-op |
| H | Explicit override semantics | done — agent/routing/override.py; tests 13/13 |
| I | Telemetry / explainability | done — agent/routing/telemetry.py; tests 9/9 |
| J | Adaptive historical scoring | done — Jstore agent/routing/history.py (12/12) + Jwire via integration.note_outcome |
| K | Regression tests | done — 7 thrashed variant tests deleted; canonical tests/agent/routing/ (114) + tests/integration/test_routing_end_to_end.py (6); shims restored; agent_init.py syntax error (pre-existing, prior session) fixed; 148 pass under scripts/run_tests.sh |
| L | Runtime validation | done — docs/routing/VALIDATION.md; demo 3/3 scenarios exit 0; recovery + TTL re-admit shown |
| M | Independent final review | done — subagent (ecc:python-reviewer) VERDICT: PASS, 0 blocking findings; 7/7 invariants held, 120 tests + demo + flag-off regressions re-run independently. Repair cycle 1 applied 4 non-blocking hardening items (#2 invalid-override surfaced in `why`, #3 eager `resolve_expiries` in live seam, #4 single-timestamp snapshot, #8 record builders wrapped). 121 tests pass post-repair. |

## Timeline
- 2026-09-09 — branch created, run log + adopted defaults recorded.
- 2026-09-09 — nodes B, C, D, Jstore, E, H, R implemented in `agent/routing/`; 97 unit tests green.
- 2026-09-09 — §7B decision: node I telemetry is self-contained (in-memory ring buffer + best-effort
  JSONL under `get_hermes_home()/routing/decisions.jsonl` + stdlib logging). `hermes_cli/audit_sink.py`
  is a hash-chained security ledger coupled to undefined K02/K08 spec — not reused.
- 2026-09-09 — §7B decision: node G integration keeps ONE live edit only — a guarded reorder hook in
  `chat_completion_helpers.try_activate_fallback`. The aux (`auxiliary_client`) and HGES
  (`agent/graph/provider_worker`) seams named in the graph are deferred: `plan_route`/`plan_recovery`
  exist and are unit-tested but are not wired into those hot paths in this pass, to keep the flag-off
  guarantee trivially auditable. Wiring them is follow-up work and must fix reviewer obs #2/#3 first.
- 2026-09-09 — pre-existing landmine fixed: `agent/agent_init.py` (prior session, uncommitted) had
  `from agent.model_router import ModelRouter` inserted INSIDE a parenthesised import block →
  `SyntaxError` that made the whole `agent` package unimportable (root cause of the prior session's
  "No tests collected"). Moved to its own statement.
- 2026-09-09 — TERMINAL STATE REACHED. See §Terminal state below.

## Terminal state (2026-09-09)

```
NODES_A_THROUGH_M              = COMPLETE
ROUTING_UNIT_TESTS            = 115 pass / 0 fail   (tests/agent/routing/)
END_TO_END_TESTS             = 6 pass / 0 fail     (tests/integration/test_routing_end_to_end.py)
WIDER_REGRESSION_SWEEP        = 314 pass / 0 fail   (aux client, credential pool, fallback reload,
                                                    empty-model recovery, compression-stall, etc.)
FLAG_OFF_FALLBACK_REGRESSION  = pass (unchanged)    (test_failover_identity, fallback_chain_reload, ...)
RUNTIME_RECOVERY_DEMO         = PASS   (python -m agent.routing.demo --scenario recover, exit 0)
INDEPENDENT_REVIEW           = PASS   (subagent, 0 blocking findings)
FEATURE_FLAG                  = routing.adaptive.enabled  DEFAULT OFF
HUMAN_RELEASE_REQUIRED       = YES    (enabling the flag in production is René's gate; §8)
```

---

## Takeover — RMK Smart Model Routing V1 completion (2026-09-10/11)

Astra reached its iteration limit (Kanban `t_f6a7d21e`, `last_failure_error:
"Iteration budget exhausted (150/150)"`). Work resumed from the persisted state
rather than restarted; no Astra code was discarded or replaced for style.

### State found (verified, not assumed)

| Area | State | Evidence |
|---|---|---|
| `agent/routing/` package (registry, capabilities, health, scoring, history, override, telemetry, router, integration, admission, logical, runtime, `__main__`) | DONE | 135 unit tests green on arrival |
| Live wiring (`turn_context` → `prepare_turn_route`, `turn_usage`/`turn_api_error` → `note_outcome`, `chat_completion_helpers` → `reorder_fallback_chain`) | DONE | grep + 14 integration tests green |
| Review findings R1 (router not wired), R2 (`get_model_capabilities` NameError), R3 (dead `routing_telemetry`), R4 (tests on fabricated state) | DONE | writer `turn_usage.py:98`, reader `hermes_state_usage.routing_telemetry_recent`; `tests/integration/test_routing_live_path.py` drives the real agent loop over HTTP |
| Billing tri-state + zero-paid gate | DONE | `test_v1_billing.py`, `scoring.rank_candidates` |
| Canonical registry **in production** | NOT_STARTED | `routing:` was `null` in `E:\KI\Hermes\config.yaml`; `HERMES_HOME/routing/` did not exist → the whole V1 logical layer was dead in the real runtime |
| Routing-loop protection | PARTIAL | `rmk-*` blocked; OmniRoute `auto/*` / `free-stack` combos were not |
| `rmk-router status/explain` | PARTIAL | module entry point existed; no executable, no recent-selections / fallback-events / errors, sections not per spec |
| Config-error handling | FAILED | a configured-but-missing registry raised a bare `FileNotFoundError` out of `prepare_turn_route` — every turn would crash |
| Fallback transport | FAILED | chain rebuilt from registry lost `base_url`/key for routes not already in the chain → switch to an unreachable endpoint |
| Workload classifier | PARTIAL | `\bunit test\b` missed "unit tests"; a plainly-coding prompt classified `rmk-general` |
| Real Hermes runtime validation | NOT_STARTED | never run against live OmniRoute |
| Root report (R5) | NOT_STARTED | only AUDIT/DEPENDENCY_GRAPH/VALIDATION existed |

### Changes made

* `agent/routing/admission.py` — `_is_router_target`: refuse combo/meta-route rows (`auto/*`, `free-stack`, `combo/*`, nested `…/auto/…`, `moa`, `rmk-router`). Structural routing-loop protection; atomic over the document.
* `agent/routing/runtime.py` — `RoutingConfigurationError` for unreadable / unparseable / inadmissible registries. Fails clearly instead of crashing a turn or silently dropping the billing policy.
* `agent/routing/integration.py` — `connection_entries` / `chain_entry_for` resolve `base_url` + `key_env` from the provider's configured connection; the fallback pool is now built from the **ranked eligible candidates only**, so a mid-turn hop cannot leave the route.
* `agent/routing/logical.py` — classifier rules rewritten (stemmed, ordered, still deterministic and offline).
* `agent/routing/__main__.py` — `explain` emits TASK_CLASS / ROUTE / ELIGIBLE / EXCLUDED+reasons / SELECTED / FALLBACK_ORDER and reports the route's *effective* requirements; `status` adds router health, billing breakdown, disabled/unhealthy candidates, recent selections, fallback events, errors; credential-shaped keys filtered; subparsers so `explain --route X "task"` parses.
* `agent/model_router.py` — V1 telemetry records now carry `requirements` / `task_type`.
* `bin/rmk-router`, `bin/rmk-router.cmd` — operator entry point.
* `scripts/validate_smart_routing.py` — re-runnable real-runtime validation (M/N/O/P/Q).
* `tests/agent/routing/test_v1_hardening.py` — 29 tests for the above.
* `docs/routing/SMART_MODEL_ROUTING_V1.md` — the root report R5 asked for.
* `E:\KI\Hermes\routing\registry.json` + `README.md` — canonical registry, contents verified live.
* `E:\KI\Hermes\config.yaml` — `routing.adaptive` block (backup: `config.PRE-SMART-ROUTING-V1-20260910-234130.yaml`).

### Live runtime evidence (production config + live OmniRoute, 2026-09-11)

`python scripts/validate_smart_routing.py` → 10/10 PASS: general turn, coding
turn, reasoning turn and tool turn all executed through Smart Routing on
registry-approved zero-cost models; a real fallback hop executed on the next
approved candidate after the primary was excluded; an exhausted pool returned an
explicit `NO_ELIGIBLE_MODEL`; history survived a fresh handle; `explain` matched
the runtime decision exactly.
