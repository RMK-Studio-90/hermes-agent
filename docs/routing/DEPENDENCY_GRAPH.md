# Adaptive Model Routing — Implementation Dependency Graph

The internal DAG this build executes. `X → Y` = Y depends on X. Every node runs
Implement → Unit test → Self-review → Repair (≤3 cycles, §6) and lands behind the
feature flag `routing.adaptive.enabled` (default **false**) so no runtime behavior
changes until nodes L + M pass (rollback = flip flag / drop `agent/routing/`).

## Edge list

```
A ─┬─> B
   ├─> C
   ├─> D
   └─> H

B ─┬─> C            (capability vocabulary)
   ├─> D            (health attaches to entries)
   ├─> E
   └─> H            (validate override target)

C ──> E
D ─┬─> E
   └─> F
Jstore ─> E         (adaptive read API; empty-safe)

E ─┬─> F
   └─> R            (R = router orchestrator)
F ──> R
H ──> R

R ──> G
R ──> I
G ─┬─> I            (telemetry sees real decisions)
   └─> Jwire        (real outcomes populate history)
I ──> Jwire

B,C,D,E,F,H,R,G,I,Jstore,Jwire ──> K
K ──> L
L ──> M
```

## Topological build order

| # | Node | Deliverable | Depends on | Unit test focus | Rollback |
|---|---|---|---|---|---|
| 1 | **A** audit | `docs/routing/AUDIT.md` | — | — (doc) | delete doc |
| 2 | **B** registry | `agent/routing/registry.py` — health/availability overlay over `agent/models_dev.py`; `HealthStatus`, `ModelEntry`, lazy static-metadata pull, explicit `register()`, `update_health(ttl)`, `is_expired`, `cost_tier` from real costs | A | empty-registry lookup pulls from models_dev; unknown model → None; free vs paid tiering; TTL expiry resets to HEALTHY; thread-safe singleton | file behind flag; `canonical_registry.py` shim re-exports |
| 3 | **C** capability classification | `agent/routing/capabilities.py` — `classify_task(prompt, attachments, tools, task_type, context_len) -> RequiredCapabilities{vision, tool_use, reasoning, min_context, long_output}` | A, B | image attachment → vision; non-empty tool list → tool_use; task_type/complexity → reasoning; token estimate → min_context; deterministic, no network | pure function; unused unless flag on |
| 4 | **D** failure classification + route health | `agent/routing/health.py` — consume `agent/error_classifier.ClassifiedError`; map `FailoverReason` → health action table (billing/rate_limit → EXCLUDE ttl; server_error/overloaded/timeout → DEGRADE ttl; model_not_found → EXCLUDE long; content_policy/context_overflow → no health change); rolling failure window per (provider,model); auto-recovery via TTL + half-open probe flag | A, B | each FailoverReason maps to expected action; N consecutive server_errors → DEGRADE; EXCLUDE expires → half-open; non-route reasons leave health untouched | flag-gated; health store in-memory, no persistence side effect |
| 5 | **Jstore** adaptive history store | `agent/routing/history.py` — SQLite table `routing_outcomes(provider,model,cap_class,ts,ok,latency_ms,reason)` under profile home; write API + `stats(provider,model,cap_class) -> {success_rate, p50_latency, n}` with recency decay; empty-safe (neutral priors) | A | empty store → neutral prior; decayed success rate; window cap; concurrent writes; `--db` honored | drop table / delete db file; reads empty-safe |
| 6 | **E** candidate filtering + scoring | `agent/routing/scoring.py` — `score(entry, required, health, hist) -> Score`; pipeline CAPABILITY (hard filter) → AVAILABILITY (health != EXCLUDED) → **FREE_FIRST** (free tier dominates) → RELIABILITY (health + hist.success_rate) → COST → LATENCY (hist.p50) → deterministic tie-break (model_id, provider); returns ranked list + per-candidate breakdown | B, C, D, Jstore | free always outranks paid when capable; EXCLUDED filtered; DEGRADED penalized not removed; capability miss removed; tie-break deterministic; empty history → health-only ordering; **no capable free model → returns paid candidates flagged `requires_paid=True` (caller decides, never silent)** | pure; flag-gated caller |
| 7 | **H** explicit override semantics | `agent/routing/override.py` — `resolve_override(config_pin, turn_override, profile_default) -> Override{target, mode}`; mode `strict` (never deviate; hard failure surfaces) vs `soft` (target first, then router fallback on FailoverReason in fallback set); validate target exists in registry; precedence turn > profile > config_pin > auto | A, B | precedence order; strict blocks fallback; soft permits fallback; unknown target → error not silent; no override → auto | pure; default mode `soft` = today's pin-then-fallback behavior |
| 8 | **R** router orchestrator | `agent/routing/router.py` — `AdaptiveRouter.select(task_ctx, override=None) -> Decision{provider, model, ranked, why}`; `select_recovery(decision, classified_error) -> Decision|None` (health-aware next pick, bounded by candidate exhaustion + caller's existing `_fallback_index`; **no parallel attempt counter**) | C,E,F,H | end-to-end select on synthetic registry; recovery skips just-failed + EXCLUDED; recovery honors strict override (returns None); exhaustion → None; `why` populated | flag-gated; not imported by runtime unless G active |
| 9 | **F** automatic provider fallback + recovery | integrate `R.select_recovery` with `agent/chat_completion_helpers.try_activate_fallback` + `agent/turn_recovery.py`; health updated from `ClassifiedError` at existing failover call sites; TTL re-admission on later turns | D, E | injected rate_limit → recovery returns different healthy free model; EXCLUDED model not retried; after TTL, model re-enters candidate set; flag off ⇒ existing path unchanged | flag guards every call site; off ⇒ byte-identical to current `try_activate_fallback` flow |
| 10 | **G** worker / runtime integration | `agent/routing/integration.py` + minimal edits at main turn (`chat_completion_helpers`/`turn_recovery`), aux (`auxiliary_client`), graph (`agent/graph/provider_worker.py`); each consults `AdaptiveRouter` only when flag on | R | each seam: flag off → existing selection; flag on → router decision; aux + graph keep own chains as ultimate fallback | per-seam flag check; revert = 3 small diffs |
| 11 | **I** telemetry / explainability | `agent/routing/telemetry.py` — `RoutingDecisionRecord` (ts, cap-class, candidates, scores, filters, health snapshot, chosen, override mode, recovery hops); sink to `hermes_logging` + optional `hermes_cli/audit_sink.py`; `hermes routing explain --last N` CLI | R, G | record shape stable; no secrets; explain renders; sink failure never breaks a turn | append-only log; disable via flag |
| 12 | **Jwire** adaptive write path | wire real turn outcomes (ok/latency/reason) from G's call sites into `history.record()` | G, I | successful turn writes ok=1+latency; failed writes ok=0+reason; cap-class tagged; flag off → no writes | flag-gated writes |
| 13 | **K** regression tests | delete `tests/agent/test_model_router*` + `tests/integration/test_routing*` variants; author `tests/agent/routing/` canonical suite + `tests/integration/test_routing_end_to_end.py`; register in `scripts/run_tests.sh` set | B–Jwire | full suite green on Python 3.14; deterministic; no network; no paid calls | tests only |
| 14 | **L** runtime validation | `python -m agent.routing.demo --scenario recover` (mirrors HGES demo) + documented manual run: flag on, force `rate_limit` on primary free model, observe auto-route to healthy free model, `hermes routing explain` shows the hop, health auto-recovers after TTL; transcript in `docs/routing/VALIDATION.md` | K | scenario exits 0; recovery + recovery-back shown | scenario script only |
| 15 | **M** independent final review | dispatch subagent reviewer(s) (`ecc:python-reviewer` + `ecc:code-reviewer`) against full diff with `docs/routing/*` as spec; PASS + no blocking findings required; repair ≤3 then §7A if unresolved | L | reviewer verdict PASS | n/a — gate |

## Invariants enforced across all nodes (§2, §10)

1. **Free-First:** no code path auto-selects a paid model when a capable free model exists and no explicit paid override is set. Violations = §7A.
2. **Flag-default-off:** `routing.adaptive.enabled` absent/false ⇒ runtime selection + fallback byte-identical to pre-change behavior. Proven by K running existing suites unchanged.
3. **No self-grading:** node M reviewer runs in a separate context and did not write the code.
4. **Determinism:** given the same registry + health + history snapshot, `select()` is deterministic (explicit tie-breakers).
5. **No silent paid / no silent drop:** "no capable free model" surfaces `requires_paid=True`; never resolved silently.
6. **Rollback proven:** each node lists its rollback; L includes a flag-off re-run showing the old path intact.
7. **Consistency for late additions (§10):** any module added mid-build (salvaged `loop/*`, `rollout/*`) gets the same unit test + review + rollback treatment before wiring.

## Salvage decisions (updated as nodes execute)

| Prior artifact | Decision | Node |
|---|---|---|
| `agent/canonical_registry.py` | rewrite → `agent/routing/registry.py`; keep old path as shim | B |
| `agent/model_router.py` | rewrite → `agent/routing/router.py` + `scoring.py`; keep old path as shim | E/R |
| `agent/error_classifier.py` | reuse as-is | D |
| `agent/models_dev.py` | reuse as-is | B |
| `agent/chat_completion_helpers.try_activate_fallback` | reuse, make health-aware | F |
| `hermes_cli/audit_sink.py` | assess for node I sink | I |
| `loop/*` | assess for node J substrate | Jstore/Jwire |
| `rollout/*` | assess for rollback tooling | K/L |
| `tests/*_router*`, `tests/*_routing*` | delete, replace | K |
| `agent/graph/*` (HGES) | reuse; add routing seam in `provider_worker.py` | G |
