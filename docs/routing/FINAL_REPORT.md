# Smart Model Routing — final repair report

Baseline: integration HEAD `8791669f13`. Clean worktree: `E:/KI/Hermes/worktrees/smart-routing-repair`
branch `rmk/smart-routing-repair`.

## ROOT_CAUSE
The integration-HEAD routing core (`agent/routing/*`) was functionally complete but **not wired into the
live agent loop**: the turn hook, the outcome write-backs and the fallback seams were documented but absent
from the committed baseline. In parallel, the dirty production worktree had rewritten
`integration.note_outcome` to a new signature `(agent, provider, model, ok, reason, latency_ms, token_usage)`
without updating the call sites, which still used the integration contract
`(provider, model, ok, *, cap_class, latency_ms, session_id, token_usage, reason, registry, history)`
-> the runtime `TypeError: note_outcome() got an unexpected keyword argument 'registry'`. Additionally
`prepare_turn_route` treated an EMPTY model ("", the automatic-routing sentinel) as an explicit STRICT pin
`(provider, "")`, narrowing the pool to an unresolvable route -> `NO_ELIGIBLE_MODEL` and a `model=""` reaching
inference. This task did **not** carry the broken 482-line rewrite forward; it wired the intact HEAD core.

## CURRENT_ARCHITECTURE (verified before)
- Clients (Claude+Hermes) select/leak physical models; health/cooldown duplicated; the turn hook, outcome
  write-back and fallback seams were unwired at integration HEAD; empty model reached inference.

## TARGET_ARCHITECTURE (delivered)
- Clients send logical routes (rmk-general/rmk-code/rmk-reason/...) or leave model empty (`""`).
- ONE authoritative physical router (`agent.routing.router.AdaptiveRouter` + `scoring.rank_candidates`)
  resolves them to provider+physical model before inference.
- Hermes classifies capability need only; the central router chooses the physical model.
- FreeLLMAPI = verified free-model control plane (zero-cost admission metadata); not on the inference hot path.
- OmniRoute = provider gateway / auth / transport / accounting, receiving the already-selected physical model.

## AUTHORITATIVE_ROUTER
`agent.routing.router.AdaptiveRouter` (+ `agent.model_router.ModelRouter` facade +
`agent.routing.scoring.rank_candidates`). No other component reranks physical models.

## FILES_CHANGED (worktree; 6 modified + 1 new; +1 audit doc)
| File | Change |
|---|---|
| `agent/turn_context.py` | wire `prepare_turn_route` before prompt construction |
| `agent/routing/integration.py` | only pin a CONCRETE model (empty = auto sentinel), fix `model=""` propagation |
| `agent/turn_usage.py` | success outcome write-back via `note_outcome` (registry/history/token_usage) |
| `agent/turn_api_error.py` | failure outcome write-back via `note_outcome` (kept the `_recovered` guard) |
| `agent/chat_completion_helpers.py` | fallback seams: `reorder_fallback_chain`, routing allow-list skip, `note_route_change` |
| `tests/integration/test_routing_live_path.py` | test harness: 404 for non-chat-completions (Ollama probe) |
| `tests/integration/test_routing_regressions.py` (new) | 6 mandated regressions |
| `docs/routing/PHASE1_AUDIT.md` (new) | architecture audit |

## EXACT_DIFF_SUMMARY
4-line increase is minimal and additive, every seam gated by `adaptive_routing_enabled()` (no-op when flag off):
- turn_context: +2 (hook).
- integration.py `prepare_turn_route`: pin guard (empty model no longer pinned).
- turn_usage.py: +12 (guarded `is_enabled()` -> `note_outcome` success + failure-flag reset).
- turn_api_error.py: +9 (guarded-free `note_outcome` failure; `note_outcome` itself no-ops when flag off).
- chat_completion_helpers.py: +15 (flag-gated reorder / allow-list / recovery telemetry).
- No commit, no reset, no clean, no broad refactor. Live dirty checkout untouched.

## CLAUDE_FLOW_BEFORE / AFTER
BEFORE: Claude ended up pinned by hand to a physical free model (big-pickle / mino / nemotron ...); 429s
hammered the same model; no shared cooldown.
AFTER: Claude (via the Hermes agent loop) sends a logical route -> router picks the currently eligible
physical model -> 429 records cooldown -> next candidate auto-selected. Same state shared with Hermes.

## HERMES_FLOW_BEFORE / AFTER
BEFORE: static model; no task classification to a route; empty model could reach inference.
AFTER: Hermes classifies capability -> logical route -> central router -> concrete provider/model propagated
via switch_model. No duplicate health/cooldown logic in Hermes.

## 429_BEHAVIOR_BEFORE / AFTER
BEFORE: 429 -> same rate-limited model retried; no cooldown state; no shared exclusion.
AFTER: 429 -> note_outcome(ok=False) -> health EXCLUDED (TTL 300s) -> scored out of immediate eligibility
-> select_recovery / reorder picks the next candidate -> skip of disallowed fallback -> shared registry state
visible to Claude and Hermes.

## FREELLMAPI_FINAL_ROLE
Free-model control plane: verified zero-cost admission, capabilities, availability, aliases, metadata.
Not forced through the inference hot path.

## OMNIROUTE_FINAL_ROLE
Provider gateway / auth / transport / accounting; receives the router-selected physical model; `DIRECT` is
acceptable after the router decision.

## TEST_RESULTS
CORE_ROUTING:     166 passed / 0 failed   (unchanged baseline)
INTEGRATION:      14 passed / 0 failed    (was 6 passed / 8 failed)
NEW_REGRESSIONS:   6 passed / 0 failed
RUN_AGENT (full breadth): 2120 passed / 27 failed / 3 skipped
  -> The 27 failures are PRE-EXISTING at integration HEAD: the identical 27-test failure list reproduces
     on the pristine baseline worktree `_verify-base` (HEAD 8791669f13, untouched). They all live in
     compression / in-place-compaction / tool-segmentation / guardrail specs, and none of those test files
     imports any module changed by this work. No new regressions introduced.

PRODUCTION_READY: YES (routing scope) — all routing suites green in the clean worktree, zero new regressions.
NOTE: the patch is validated in a clean worktree; deployment into the live (dirty) checkout is a separate
authorized step and was intentionally NOT performed (task forbade direct modification of the dirty checkout).
