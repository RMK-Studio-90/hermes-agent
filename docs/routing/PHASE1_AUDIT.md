# Smart Model Routing — Phase 1 architecture audit

Baseline: integration HEAD `8791669f13` (`rmk/integration-current-upstream`), verified via a clean
worktree `E:/KI/Hermes/worktrees/smart-routing-repair`. Branch `rmk/smart-routing-repair`.

## CLAUDE_REQUEST_FLOW (as it runs through the Hermes agent loop)
```
Claude / client
  -> endpoint acts on the SAME Hermes agent loop (AIAgent.run_conversation)
  -> agent/turn_context.build_turn_context
      -> agent/routing/integration.prepare_turn_route   (NEWLY WIRED — was missing at HEAD)
          -> classify_workload(user_message, route=adaptive.route|None)      => logical route
          -> capabilities.classify_task(prompt, history, images, tools)      => RequiredCapabilities
          -> runtime.configured_router(config)                               => canonical registry
          -> ModelRouter.select_workload / select_model                       => AdaptiveRouter.select
               -> agent.routing.router.AdaptiveRouter.select                 => Decision (physical model)
          -> switch_model(decision.model, decision.provider, ...)           -> agent.model/provider set
  -> normal turn proceeds on the chosen physical model
```
Decision owner: **`agent.routing.router.AdaptiveRouter`/`agent.routing.scoring.rank_candidates`**.

## HERMES_REQUEST_FLOW
```
Hermes
  -> task classification (what capability does this task need?)
  -> logical route (rmk-general | rmk-code | rmk-reason | ...)            [Hermes' responsibility — capability intent]
  -> central router resolves physical model                                [router responsibility — not Hermes]
  -> OmniRoute / provider gateway carries the selected physical model
```
Hermes keeps a logical "what kind of model" decision. The physical selection is handed to the Smart Router.

## FREELLMAPI_ROLE
`agent/` FreeLLMAPI / rmk-free-model integration is **not** on the inference hot path here. The hot path is
`agent.routing.*` (registry = verified free-model capability/billing metadata; health/scoring = selection).
FreeLLMAPI contributes the **verified free-model control-plane evidence** (zero-cost admission,
capabilities, availability); inference does not flow through it. No change forced.

## OMNIROUTE_ROLE
OmniRoute remains the provider gateway / transport / auth / accounting. It receives a concrete physical
model **after** the router selects it. `DIRECT` at OmniRoute is legitimate once the router has decided.

## PHYSICAL_MODEL_DECISION_POINTS (authoritative)
1. `agent/routing/router.py::AdaptiveRouter.select` + `.select_recovery`  — THE single rank decision
2. `agent/routing/scoring.py::rank_candidates`                          — capability/availability/billing ranking
3. `agent/routing/registry.py::RouteRegistry`                            — health + billing admission state
4. `agent/routing/integration.py::prepare_turn_route`                    — wires decision into agent.model
No other component reranks physical models.

## AUTHORITATIVE_PHYSICAL_ROUTER
**`agent.routing.router.AdaptiveRouter` (+ `agent.model_router.ModelRouter` facade + `agent.routing.scoring`)**
is the ONLY authoritative physical-model selection layer.

## SHARED_STATE_LOCATIONS
- In-process singleton: `agent.routing.registry.registry` (= `integration._default_registry`),
  shared by every client (Claude + Hermes) in the same Hermes profile.
- `agent.routing.runtime.configured_router(config)` — profile-scoped cache; reload preserves operational
  cooldown (`health_status/reason/since/ttl/consecutive_failures/half_open`) across registry reloads.
- Durable shared output store: `HERMES_HOME/routing/outcomes.db` (+ `decisions.jsonl`) — collision-free
  multi-writer via SQLite WAL.
- `HERMES_HOME/routing/registry.json` — canonical verified free-model admission document (billing,
  logical_routes, capabilities).
No second parallel database was created (task constraint honoured). Both Claude-bound and Hermes-bound
requests share this single profile-scoped state, so a 429 recorded by either is visible to both.

## DUPLICATE_ROUTING_LOGIC (removed/mitigated)
- Root cause of the runtime TypeError: the dirty working tree had rewritten `integration.note_outcome` to a
  new signature `(agent, provider, model, ok, reason, latency_ms, token_usage)` while the two call sites
  (`agent/turn_usage.py`, `agent/turn_api_error.py`) still used the integration HEAD contract
  `(provider, model, ok, *, cap_class, latency_ms, session_id, token_usage, reason, registry, history)`.
  At integration HEAD the contract is already consistent; the dirty rewrite (482 changed lines) was the
  only thing that broke it. This work does **not** carry that rewrite forward.
- `model=""` propagation gap: `prepare_turn_route` treated an EMPTY model (`""`, the automatic-routing
  sentinel) as an explicit STRICT pin `(provider, "")`, narrowing the pool to an unresolvable route and
  yielding `NO_ELIGIBLE_MODEL` before a physical model was selected. Fixed by only pinning a *concrete*
  (non-empty) model.
- The turn hook (`turn_context -> prepare_turn_route`) and the outcome write-back (`turn_usage` /
  `turn_api_error -> note_outcome`) and the fallback seams (`chat_completion_helpers ->
  reorder_fallback_chain` / `note_route_change` / routed allow-list) existed in the dirty tree but were
  **absent** from the committed integration baseline. This work wires exactly those seams onto the intact
  HEAD core (minimal patch, no architecture change).

## Verified behaviour after the repair
- Core: `tests/agent/routing` 166 passed.
- Integration: `test_routing_end_to_end` + `test_routing_live_path` 14 passed (was 6 passed / 8 failed).
- New regressions: `test_routing_regressions` 6 passed (RATE_LIMIT_COOLDOWN, CROSS_REQUEST_COOLDOWN,
  LOGICAL_ROUTE_RESOLUTION, MANUAL_OVERRIDE, BILLING_FAIL_CLOSED, OUTCOME_API).
