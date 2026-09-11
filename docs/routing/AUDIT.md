# Node A — Baseline / Routing Architecture Audit

Date: 2026-09-09. Scope: `hermes-agent` model/provider selection + fallback, as of branch
`feat/adaptive-model-routing` (carrying prior uncommitted work).

## A.1 Existing production mechanisms (KEEP — integrate, do not replace)

| Concern | Module / symbol | Notes |
|---|---|---|
| Model metadata (canonical source) | `agent/models_dev.py` — `ModelInfo`, `ProviderInfo`, `ModelCapabilities`, `get_model_info()`, `get_model_capabilities()` | models.dev registry w/ disk cache, etag, stale-grace, corruption quarantine. Cost, context window, modalities, tool_call, reasoning, family. **Already the canonical registry for static metadata.** |
| Error to recovery classification | `agent/error_classifier.py` — `FailoverReason` (26 variants), `ClassifiedError{reason,status_code,provider,model,message,error_context}` | Full failure taxonomy already exists (auth, billing, rate_limit, upstream_rate_limit, overloaded, server_error, timeout, model_not_found, context_overflow, content_policy_blocked, ...). **Node D failure classification is essentially done at the error level.** |
| Main-turn fallback chain | `agent/agent_init.py` — `_init_fallback_chain()`, `_fallback_entries()`; `agent/chat_completion_helpers.py` — `try_activate_fallback(agent, reason)`, `_fallback_chain`, `_fallback_index`, `_RATE_LIMIT_FAILOVER_REASONS` | Bounded, index-walked chain from config `fallback_model` / `fallback_providers`. Rate-limit/billing get immediate rotate; transient transport rebuilds client first. |
| Fallback decision point | `agent/turn_recovery.py` (`_should_fallback`, guards on `_fallback_index < len(_fallback_chain)`), `agent/turn_empty_response.py`, `agent/conversation_compression.py` | Where a classified error becomes a fallback attempt during a live turn. |
| Credential-pool rotation | `agent/agent_runtime_helpers.py` (~74 fallback refs) — pool refresh, exhaustion, primary-vs-fallback pool guards, reset-aware restore | Distinct layer *below* model fallback: rotates keys within a provider before switching models. |
| Aux-role fallback | `agent/auxiliary_client.py` — `_try_configured_fallback_chain()`, `_try_main_fallback_chain()`, `get_fallback_chain()` | Separate chain for auxiliary roles (MoA, summarizers, graph roles). Reads `hermes_cli/fallback_config.py`. |
| User fallback policy | `hermes_cli/fallback_config.py` — `get_fallback_chain()`; `hermes_cli/fallback_cmd.py` | User-configured ordered fallback list. |
| Config defaults | `hermes_cli/config_defaults.py` — `model`, `providers`, `fallback_providers`, `aux_same_provider_retries`, `auxiliary.*`, and (prior session) `graph.budget`, `graph_<role>` | Free-First expectation lives here + AGENTS.md. |
| HGES graph runtime | `agent/graph/*` (20 modules) + `hermes_cli/graph.py` — `provider_worker.py`, `runner.py`, `request.py`, `models.py`, `budget.py`, `windows_job.py` | Prior session's bounded role-graph executor. Uses existing provider resolution. Own budget/timeout/process-tree kill. Node G integration target for graph-mode routing. |
| Observability | `hermes_cli/observability/shared_metrics.py`, `hermes_logging.py`, `hermes_state_usage.py` (`record_auxiliary_usage`, `update_token_counts`, `update_session_billing_route`) | Token/billing telemetry exists; **no per-(provider,model) outcome/latency history table.** |

## A.2 Prior session's parallel reimplementation (SALVAGE selectively)

| File | Verdict | Action |
|---|---|---|
| `agent/canonical_registry.py` | Partial. Good idea (health layer over `models_dev`), but: `initialize()` no-ops (never preloads) so `filter_models_by_capabilities()` iterates an **empty `_models` dict** on a fresh process. `datetime.utcnow()` deprecated on 3.14. Health TTL logic sound. | **Rewrite** as thin health/availability overlay that pulls static data lazily from `models_dev` on lookup AND supports explicit registration; keep `HealthStatus`, `ModelEntry`, `update_health_status(ttl)`, `is_expired()`. |
| `agent/model_router.py` | Shallow / buggy. Scoring = `reliability*10 + cost`; latency hardcoded 0; no adaptive history (J). `select_fallback_model` does `agent._fallback_attempts += 1` on a shared attr, never reset per request. `user_override` returns unavailable models with no health check/telemetry. Duplicates `try_activate_fallback`. | **Rewrite** scoring (E/J), delete parallel fallback counter, delegate fallback to `try_activate_fallback` + health (F). Keep the CAPABILITY to AVAILABILITY to RELIABILITY to COST to LATENCY pipeline shape. |
| `agent/agent_init.py:32` `from agent.model_router import ModelRouter` | Imported, **never called**. | Wire properly in node G or remove. |
| `hermes_cli/{operating_brain,promotion_engine,audit_sink,eval_schema}.py`, `loop/*`, `rollout/*`, `experiments/*`, `golden_set/harness.py` | Reference undefined spec sections (K02/K08/BUILD-04 §17). Large, unwired. `audit_sink.py` (577 L) plausible node I sink; `loop/*` plausible node J substrate; `rollout/*` plausible rollback path. | **Assess per node** as B–L reach them. Do not delete; do not assume correct. |
| `tools/delegate_tool.py` +28 L "operating brain" gate | Flag-gated (`delegation.operating_brain.enabled` absent → baseline). Refers to K08 §17. | Leave inert (flag absent). Out of routing scope unless a node needs it. |
| `tests/agent/test_model_router{,_fixed,_fixed2,_final}.py`, `tests/integration/test_routing{,_integration,_integration_fixed}.py` | `pytest --collect-only` → **"No tests collected"** for all. Divergent thrashed variants, one 0 bytes. | Node K: **delete all variants, author one canonical suite.** |

## A.3 Gaps (net-new work)

- **C — task capability classification:** nothing maps a *turn/task* (prompt + attachments + tool set + task_type) to a required-capability set. `model_router` takes a pre-built `required_capabilities` dict from nowhere.
- **D — route health aggregation:** `ClassifiedError` exists per call; nothing aggregates a stream of them per (provider, model) into HEALTHY/DEGRADED/EXCLUDED with decay + auto-recovery.
- **E — scoring:** real multi-factor score (capability fit, health, cost tier, latency p50, historical success) with deterministic tie-break, replacing the stub.
- **F — recovery:** health-aware candidate skip + TTL-based automatic re-admission (probe / half-open). Today a rate-limited model is only avoided within one turn's `_fallback_index` walk; next turn it is tried first again.
- **G — runtime integration:** single seam that main turn (`chat_completion_helpers`/`turn_recovery`), aux (`auxiliary_client`), and graph (`agent/graph/provider_worker`) all consult, without regressing their existing chains.
- **H — override semantics:** precedence contract for config `model` pin vs per-turn override vs profile vs router auto; "override but still fall back on hard failure" vs "override, never deviate".
- **I — explainability:** structured, queryable "why this model" record per decision (candidates considered, scores, filters applied, health snapshot).
- **J — adaptive history:** persistent per-(provider,model,capability-class) rolling success rate + latency, feeding E. New store.
- **L — runtime validation:** end-to-end demo that an injected failure (forced `rate_limit` on the primary free model) auto-routes to a healthy free model, telemetry records it, and health auto-recovers after TTL.

## A.4 Free-First constraint (AGENTS.md)

`cost_tier == "free"` (both input & output cost 0 in `models_dev`) MUST outrank any paid model regardless of other scores unless (a) no free candidate satisfies the required capabilities, or (b) an explicit override names a paid model. Any path that would auto-select a paid model without (a)/(b) = §7A hard stop.

## A.5 Integration seam decision

Introduce `agent/routing/` package (new, clean namespace) rather than continuing `agent/model_router.py` + `agent/canonical_registry.py` in place:

- `agent/routing/registry.py`     — node B (health overlay over `models_dev`)
- `agent/routing/capabilities.py` — node C
- `agent/routing/health.py`        — node D
- `agent/routing/scoring.py`       — node E + J
- `agent/routing/router.py`        — orchestrator: C→B→D→E→(H)→pick; node F recovery
- `agent/routing/history.py`       — node J store
- `agent/routing/telemetry.py`     — node I (may wrap `hermes_cli/audit_sink.py`)
- `agent/routing/integration.py`   — node G adapters for main / aux / graph

`agent/model_router.py` + `agent/canonical_registry.py` become thin shims re-exporting from `agent/routing/`
(keeps prior imports/tests working during transition), then removed in node K cleanup.
