# RMK Smart Model Routing V1 — root report

Status date: 2026-09-11. Branch `feat/adaptive-model-routing`.
Supersedes the deliverable that the independent review (`t_da598b30`) rejected
with **FAIL**; remediation task `t_f6a7d21e`.

---

## ARCHITECTURE_AFTER

```
TASK
 └─ agent/turn_context.py                     (single hook, every turn)
     └─ agent/routing/integration.prepare_turn_route
         ├─ agent/routing/logical.classify_workload      -> logical route
         ├─ agent/routing/capabilities.classify_task     -> required capabilities
         ├─ agent/routing/runtime.configured_router      -> canonical registry
         └─ agent/model_router.ModelRouter.select_workload
             └─ agent/routing/router.AdaptiveRouter.select
                 ├─ hard gates   (billing, enabled, available, route, capability)
                 ├─ agent/routing/scoring.rank_candidates (health + history + route policy)
                 └─ Decision  -> agent.switch_model()  -> OmniRoute -> provider
```

Layer separation, as required:

| Layer | Owns | Does **not** own |
|---|---|---|
| **OmniRoute** (`127.0.0.1:20128`) | provider gateway, credential/connection handling, request execution | which model a task gets |
| **RMK Model Registry** (`HERMES_HOME/routing/registry.json`) | canonical model metadata, capabilities, billing class, route eligibility, ranking metadata | selection logic, runtime health |
| **RMK Smart Model Router** (`agent/routing/`) | workload classification, eligibility gating, ranking, health, bounded fallback, telemetry | executing requests, provider credentials |
| **Hermes / agents** | consuming stable logical routes | picking concrete models |

There is exactly one routing decision layer. `agent/canonical_registry.py` is a
re-export shim over `agent/routing/registry.py`, not a second registry.
OmniRoute's own `auto/*` combos are structurally excluded (see LOOP_PROTECTION),
so the gateway cannot become a second decision layer underneath this one.

## CANONICAL_ROUTER

* Authoritative entry point: **`agent.routing.integration.prepare_turn_route(agent, user_message, conversation_history)`**, called once per turn from `agent/turn_context.py:792`.
* Decision engine: `agent.routing.router.AdaptiveRouter.select` / `.select_recovery`.
* Facade used by the turn hook: `agent.model_router.ModelRouter.select_workload`.
* Mid-turn recovery seam: `agent.chat_completion_helpers.try_activate_fallback` → `integration.reorder_fallback_chain`.
* Outcome write-back: `agent/turn_usage.py` (success) and `agent/turn_api_error.py` (failure) → `integration.note_outcome`.

## MODEL_REGISTRY

Canonical location: **`HERMES_HOME/routing/registry.json`** (`E:\KI\Hermes\routing\registry.json`),
pointed at by `routing.adaptive.registry` in `config.yaml`. Documented in
`E:\KI\Hermes\routing\README.md`.

Represented per model: provider, model_id, connection_id, enabled, available,
billing_class, cost_kind, context window, max output tokens, tool support,
structured/JSON output support, vision, reasoning, coding suitability, research
suitability, latency class, priority, logical-route eligibility, last_verified.
Runtime state (health, consecutive failures, cooldown TTL, half-open probe,
historical success/latency) is deliberately *not* in the file — it lives in the
process overlay plus `HERMES_HOME/routing/outcomes.db`, so a metadata reload
cannot erase an active cooldown (`runtime.configured_router` carries health
forward across reloads).

Ordinary model maintenance is a JSON edit. No Python change is required to add,
remove, disable, re-rank or re-classify a model. Admission
(`agent/routing/admission.parse_registry`) validates the document and applies it
atomically; an invalid document is refused and the previous registry stays live.

Contents were verified against live OmniRoute on 2026-09-10: six
`cc/claude-*` models confirmed by real completion + tool call; two `cx/gpt-5.6-sol-*`
rows admitted with `available: false` (Codex quota exhausted, HTTP 429, reset
~102h). The `-high`/`-xhigh` effort variants that `/v1/models` advertises were
**not** admitted — they return `Model '…' is not available in the active live
catalog` at call time.

## ROUTING_POLICY — logical routes

Classification (`agent/routing/logical.py`) is deterministic, offline and returns
**only** a logical route; it can never name a concrete model. An explicit route
override remains available (`--route`, `agent._routing_logical_route`,
`routing.adaptive.route`) and is validated against the route set.

| Route | Purpose | Capability requirements | Eligible candidates | Hard exclusions | Ranking | Fallback | Failure |
|---|---|---|---|---|---|---|---|
| `rmk-fast` | short, latency-sensitive turns (translate, extract, summarise, rename) | task-derived only | rows listing `rmk-fast` | not zero-cost; disabled; unavailable; excluded-health; missing task capability | latency asc, then priority desc | next eligible candidate, same route | explicit `NO_ELIGIBLE_MODEL` |
| `rmk-general` | default conversational work; anything with no clear signal | task-derived only | rows listing `rmk-general` | as above | priority desc, then latency asc | same | same |
| `rmk-reason` | architecture, trade-offs, proofs, complex analysis | forces `reasoning` | rows listing `rmk-reason` | as above **+ missing reasoning** | context window desc, priority desc, latency asc | same | same |
| `rmk-code` | implementation, refactoring, debugging, tests | forces `structured_output` | rows listing `rmk-code` | as above **+ no verified structured output** | coding score desc, priority desc, latency asc | same | same |
| `rmk-research` | literature, sources, citations, surveys | forces `reasoning` | rows listing `rmk-research` | as above **+ missing reasoning** | context window desc, research score desc, priority desc, latency asc | same | same |
| `rmk-vision` | any turn carrying images | forces `vision` | rows listing `rmk-vision` | as above **+ missing vision** | priority desc, latency asc | same | same |

Ranking is lexicographic and health-first in every route: effective health, then
recency-decayed historical success rate, then the route-specific preference
above, then a deterministic `(model_id, provider)` tie-break. Identical inputs
always produce an identical order.

## FREE_FIRST_POLICY / zero-paid hard gate

Three billing classes: `ZERO_ADDITIONAL_COST`, `METERED_PAID`, `UNKNOWN`.
`UNKNOWN` is **not** treated as free.

| Class | Zero-paid route |
|---|---|
| `ZERO_ADDITIONAL_COST` | eligible, subject to every other gate |
| `METERED_PAID` | blocked |
| `UNKNOWN` | blocked |

Enforcement points — all of them, so no single path can be the hole:

1. `scoring.rank_candidates` — hard reject `BILLING_NOT_ZERO_COST` before scoring, so no score can outrank the gate.
2. `router.AdaptiveRouter._auto` — filters the candidate pool and reports every billing rejection in `why`.
3. `router.AdaptiveRouter.select` — an explicit **override** whose target is not zero-cost returns `NO_ELIGIBLE_MODEL`; a pin narrows the pool, it never bypasses the gate.
4. `router.AdaptiveRouter.select_recovery` — the same filter applies to every fallback hop.
5. `integration.reorder_fallback_chain` — only zero-cost routes enter `_routing_allowed_routes`; `chat_completion_helpers` refuses to activate anything outside that set.
6. `admission.parse_registry` — refuses `ZERO_ADDITIONAL_COST` on a row whose `cost_kind` contradicts it.

When every permitted candidate is out, the request **fails** with
`NO_ELIGIBLE_MODEL` / `exhausted=True`. There is no path that silently falls
through to a metered provider. Config sets `allow_paid: false`; no new paid
credential was introduced.

## HEALTH_POLICY

`agent/routing/health.py` maps a classified failure to a health transition:
`HEALTHY` → `DEGRADED` (usable, deprioritised) → `EXCLUDED` (not selectable) with
a per-reason TTL. Tracked per `(provider, model_id)`: consecutive failures,
reason, since-timestamp, TTL, half-open flag.

* A single transient failure degrades but does not permanently disable a candidate; `note_success` clears the state immediately.
* When an `EXCLUDED` TTL lapses, `resolve_expiries` re-admits the route as half-open `HEALTHY` — one probe; success closes it, failure re-excludes it.
* `enabled` and `available` are operator/registry hard gates and are *not* touched by health, so health recovery can never resurrect a model an operator disabled.
* Historical success and p50 latency are recency-decayed per capability class in `HEALTH`-independent storage (`routing/outcomes.db`), and survive restart.

## FALLBACK_POLICY

* Fallback stays inside the selected route's approved candidate set. `prepare_turn_route` rebuilds `agent._fallback_chain` from **the ranked eligible candidates only**, so a mid-turn hop cannot land on a model the route excluded.
* Ordering is deterministic: the eligible ranking minus whatever already failed.
* Bounded: `select_recovery` takes an explicit hop budget (default = pool size) and the caller-supplied `tried` set; there is no private attempt counter that can strand state across turns.
* Chain entries carry real transport (`base_url`, `key_env`) resolved from the provider's configured connection, so a fallback target is actually reachable.

## LOOP_PROTECTION

A request has a bounded routing path because a registry row can only name a
**terminal execution target**. `admission._is_router_target` refuses:

* this router's own logical routes (`rmk-*`),
* OmniRoute meta-routes and combos (`auto/*`, `free-stack`, `combo/*`, `router/*`, and those nested behind a prefix such as `no-think/auto/best-chat`),
* router-shaped providers (`rmk`, `rmk-router`, `moa`, `auto`).

Rejection is atomic over the whole document, so a single bad row cannot be
partially admitted. Consequences: the router cannot select a selector; OmniRoute
cannot route back into a router; there is no nested combo recursion; aliases
cannot resolve recursively. Fallback cycles are prevented separately by the hop
budget plus the `tried`/`exclude` set.

## TELEMETRY_SOURCE

`agent/routing/telemetry.py` emits a structured `RoutingDecisionRecord` for every
selection, recovery hop, health change and outcome to three sinks:

* stdlib logger `hermes.routing` (INFO),
* `HERMES_HOME/routing/decisions.jsonl` (best effort, append-only),
* an in-process ring buffer read by `rmk-router status`.

Durable per-session rows additionally go to the `routing_telemetry` table
(writer `agent/turn_usage.py:98`, reader `hermes_state_usage.routing_telemetry_recent`,
surfaced via `telemetry.recent`). Records carry routes, scores, health and
failure *reasons* only — never prompt text, credentials or response bodies. Every
sink write is wrapped; a telemetry failure can never break a turn.

## Operator surface

```
rmk-router status            # router health, logical routes, registry, candidate
                             # ordering, disabled/unhealthy candidates, recent
                             # selections, fallback events, errors
rmk-router explain "<task>"  # TASK_CLASS, ROUTE, ELIGIBLE, EXCLUDED + reasons,
                             # SELECTED, FALLBACK_ORDER
rmk-router explain --route rmk-code --tools "<task>"
```

Tracked implementation: `scripts/rmk-router` (+ `.cmd`), wrapping
`python -m agent.routing`. `bin/rmk-router*` are convenience shims (`/bin/` is
gitignored), and `E:\KI\Hermesinmk-router.cmd` puts the command on PATH
system-wide. Output is JSON, read-only, and filtered against credential-shaped
keys. `status` reads the durable `decisions.jsonl` when the calling process has
no in-memory ring buffer, so a fresh operator shell still sees real recent
activity. No SQLite access is needed to inspect routing.

## KNOWN_BEHAVIOURS / RISKS

* **Reliability precedes the route preference.** Ranking is health → recency-decayed historical success → route-specific preference. A candidate with proven successes (success rate 1.0) therefore outranks an unproven one (neutral prior 0.5) even on `rmk-fast`, where the unproven model advertises lower latency. This is Astra's documented design (`scoring.py`) and is deliberate — an unproven model should not win on a self-declared number — but it means a freshly added "fast" model needs some successful turns before it wins its own route. Observable in `rmk-router explain --route rmk-fast`.
* **`available: false` is a hard gate, not health.** The two Codex rows will not be re-admitted automatically when the quota window resets; an operator (or the future Control Plane) flips the flag.
* **Registry accuracy is a human/Control-Plane responsibility.** Admission validates shape and internal consistency, not truthfulness: a wrong `billing_class` in the file is a wrong policy. The `-high` effort variants are the cautionary case — the provider catalog advertised models that do not execute.
* **The classifier is keyword-based**, deliberately conservative and offline. Unusual phrasing falls back to `rmk-general`, which is safe (general candidates are a superset in the current registry) but not always optimal. `--route` / `agent._routing_logical_route` remains the explicit escape hatch.
* **LM Studio (`127.0.0.1:1234`) was down** during validation and is not exposed through OmniRoute, so no local zero-cost model is in the registry. Local models are the natural resilience layer if every subscription quota is exhausted simultaneously; adding one is a registry edit once LM Studio is running.

## Interface for the deferred Free Model Control Plane

Out of scope for V1 (explicitly deferred): continuous global free-model
discovery, automatic external pricing watchers, free→paid catalog watchers,
autonomous addition of newly discovered provider models, continuous external
combo synchronisation.

The clean seam left for it: the Control Plane writes `registry.json` and nothing
else. `runtime.configured_router` reloads on mtime/size change and carries live
health forward, so a rewrite takes effect on the next turn without a restart and
without a router change. Contract in `E:\KI\Hermes\routing\README.md`:
complete document, atomic write, never claim `ZERO_ADDITIONAL_COST` without
evidence, never emit a combo/meta-route id, never try to express runtime health.

## ROLLBACK_PATH

1. **Instant, no deploy:** set `routing.adaptive.enabled: false` in `E:\KI\Hermes\config.yaml` (or export `HERMES_ROUTING_ADAPTIVE=0`, which overrides config). Every seam becomes a no-op and Hermes returns to its previous selection and fallback behaviour.
2. **Config restore:** `E:\KI\Hermes\config.PRE-SMART-ROUTING-V1-20260910-234130.yaml` is the pre-change production config.
3. **Full removal:** delete `agent/routing/`, `tests/agent/routing/`, `tests/integration/test_routing_end_to_end.py`, `tests/integration/test_routing_live_path.py`, `scripts/validate_smart_routing.py`, `bin/rmk-router*`, `E:\KI\Hermes\routing\`; revert the hunks in `agent/chat_completion_helpers.py`, `agent/turn_context.py`, `agent/turn_usage.py`, `agent/turn_api_error.py`; drop `agent/model_router.py` and `agent/canonical_registry.py` once their name-only importer (`agent/agent_init.py:32`) is dropped.

None of the work is committed: everything is untracked/modified in the working
copy on `feat/adaptive-model-routing`, so `git checkout -- <path>` / deleting the
untracked files is a complete revert of the code half.
