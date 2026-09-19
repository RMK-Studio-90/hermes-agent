"""Node G — worker / runtime integration.

The routing package is inert until ``routing.adaptive.enabled`` is true. This
module is the runtime seam. Entry points are no-ops when the flag is off.
Automatic selection fails closed when no compatible route exists; telemetry
writes remain best-effort.

Integration points
------------------
* ``prepare_turn_route`` — called before the conversation prompt is built.
* ``reorder_fallback_chain(agent, reason)`` — called once at the top of
  ``agent.chat_completion_helpers.try_activate_fallback``. When enabled it
  (a) feeds the just-failed route into health, and (b) reorders the *unwalked*
  tail of ``agent._fallback_chain`` by health + recent success so the existing
  swap machinery picks the best compatible next entry. Rejected entries remain
  in the configured chain for later cooldown recovery but cannot be activated.
* ``plan_route`` / ``plan_recovery`` — richer facade for the auxiliary-client
  and HGES ``provider_worker`` seams and for the end-to-end demo.
* ``note_outcome`` — node J write path: records the result of a routed call.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent.routing._flags import adaptive_routing_enabled
from agent.routing.capabilities import RequiredCapabilities, classify_task
from agent.routing.history import RoutingHistory, history as _default_history
from agent.routing.override import resolve_override
from agent.routing.registry import RouteRegistry, registry as _default_registry
from agent.routing.router import AdaptiveRouter, Decision, RouteContext
from agent.routing.scoring import rank_candidates

_LOG = logging.getLogger("hermes.routing")
Route = Tuple[str, str]
MANUAL_MODEL_PIN_KEY = "manual_model_pin"


def normalize_manual_model_pin(value: Any) -> Optional[Route]:
    """Return the persisted/runtime manual pin as a normalized route."""
    if isinstance(value, dict):
        provider, model = value.get("provider"), value.get("model")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        provider, model = value
    else:
        return None
    route = (str(provider or "").strip().lower(), str(model or "").strip())
    return route if route[1] else None


def manual_model_pin(agent: Any) -> Optional[Route]:
    """The explicit user-owned model route, if this session has one."""
    return normalize_manual_model_pin(
        getattr(agent, "_routing_manual_model_override", None))


def set_manual_model_pin(agent: Any, provider: Any, model: Any) -> Optional[Route]:
    """Stamp explicit user model intent; generic/internal switches never call this."""
    pin = normalize_manual_model_pin((provider, model))
    agent._routing_manual_model_override = pin
    if pin is not None:
        agent._routing_explicit_model = True
    return pin


def clear_manual_model_pin(agent: Any) -> None:
    """Release explicit user model intent so profile/adaptive routing may govern."""
    agent._routing_manual_model_override = None
    agent._routing_explicit_model = False


def is_enabled() -> bool:
    return adaptive_routing_enabled()


def note_route_change(agent: Any, previous: Route, reason: Any) -> None:
    """Record a completed runtime swap, not merely a proposed candidate."""
    if not adaptive_routing_enabled():
        return
    from dataclasses import asdict
    from agent.routing import telemetry
    required = getattr(agent, "_routing_required", None)
    telemetry._emit(telemetry.RoutingDecisionRecord(
        ts=telemetry._iso_now(), event="recovery", chosen=[agent.provider, agent.model],
        previous_route=list(previous), trigger=getattr(reason, "value", None),
        recovery_hops=agent._fallback_index, session_id=agent.session_id,
        requirements=asdict(required) if required else None,
        task_type=getattr(agent, "platform", None) or "chat",
    ))


def prepare_turn_route(agent: Any, user_message: Any, conversation_history: Any) -> None:
    """Choose before prompt construction, preserving explicit pins and cached sessions."""
    if not adaptive_routing_enabled():
        return
    from agent.model_router import ModelRouter
    from agent.routing.capabilities import classify_task
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly() or {}
    adaptive = (config.get("routing") or {}).get("adaptive")
    agent._routing_allow_paid = isinstance(adaptive, dict) and adaptive.get("allow_paid") is True

    messages = list(conversation_history or []) + [{"content": user_message}]
    has_images = any(
        isinstance(part, dict) and part.get("type") in {"image_url", "image", "input_image"}
        for message in messages if isinstance(message, dict)
        for part in (message.get("content") if isinstance(message.get("content"), list) else [])
    )
    required = classify_task(
        prompt=str(user_message or ""),
        history_chars=len(str(conversation_history or "")) + len(str(getattr(agent, "_cached_system_prompt", "") or "")),
        has_images=has_images, force_tools=bool(getattr(agent, "tools", [])),
    )
    agent._routing_required = required
    concrete = str(getattr(agent, "model", "") or "").strip()
    primary = (str(getattr(agent, "provider", "") or "").strip().lower(), concrete) if concrete else None
    from agent.routing.profile import configured_routing_profile
    configured_profile = configured_routing_profile(config)
    explicit_pin = manual_model_pin(agent)
    # Explicit /model intent is independent of the currently resolved runtime:
    # if fallback/recovery moved the backend, the next routing pass still starts
    # from the user's requested route. A routing profile otherwise stays fully
    # adaptive across turns; generic switch_model() calls never create this pin.
    if explicit_pin is not None:
        pinned = explicit_pin
    elif configured_profile:
        pinned = None
    elif bool(concrete) and getattr(agent, "_routing_explicit_model", True):
        pinned = primary
    else:
        # Legacy non-profile sessions keep their established prompt/runtime pair.
        pinned = primary if (primary and (
            bool(conversation_history) or bool(getattr(agent, "_cached_system_prompt", None)))) else None
    from agent.routing.runtime import configured_router
    runtime = configured_router(config)
    if runtime is not None:
        from agent.routing.logical import classify_workload
        adaptive_route = adaptive.get("route") if isinstance(adaptive, dict) else None
        # ``rmk-smart`` is the auto-classifying profile, not a logical workload
        # route.  It must therefore leave ``route`` unset so classify_workload()
        # derives a concrete route from the current turn.  Named profiles pin a
        # workload class; the legacy adaptive.route remains the fallback when no
        # selectable profile is active.
        if configured_profile:
            logical = None if configured_profile == "rmk-smart" else configured_profile
        else:
            logical = adaptive_route
        workload = classify_workload(str(user_message or ""), route=logical, vision=has_images)
        decision = ModelRouter().select_workload(
            agent, runtime, required, workload, pinned=pinned)
        if decision.route is None:
            error_code = decision.why.get("error", "NO_ELIGIBLE_MODEL")
            raise ValueError(f"{error_code}: {decision.why}")
        # Execution keeps Hermes' canonical provider lifecycle. Only the pool
        # and order come from Smart Routing; no gateway combo is introduced.
        existing = {(str(e.get("provider") or "").strip().lower(),
                     str(e.get("model") or "").strip()): e
                    for e in agent._fallback_chain if isinstance(e, dict)}
        connections = connection_entries(config)
        # Only the *eligible* candidates for this route are considered, so a
        # mid-turn hop can never land on a model this route excluded.
        eligible = [c.route for c in decision.ranked] or [decision.route]
        # P1-1 Layer A — candidate connection admission: a candidate whose
        # provider transport cannot be resolved must be rejected before
        # provider execution instead of crashing switch_model mid-turn.
        admitted, admission_rejects = admit_candidate_connections(
            eligible, existing, connections,
            str(getattr(agent, "provider", "") or ""))
        for row in admission_rejects:
            _note_connection_rejection(agent, (row["route"][0], row["route"][1]),
                                       registry=runtime.registry)
        # P1-1 Layer B — switch_model fail-safe: even an admitted candidate
        # can fail endpoint resolution at switch time; the guard skips it,
        # records the reason and continues failover (never raises out).
        executed: Optional[Route] = None
        guard_rejects: List[Dict[str, Any]] = []
        if admitted:
            executed, guard_rejects = activate_admitted_candidate(
                agent, admitted, primary, existing, connections)
            for row in guard_rejects:
                _note_connection_rejection(agent, (row["route"][0], row["route"][1]),
                                           registry=runtime.registry)
        rejects = admission_rejects + guard_rejects
        if executed is None:
            # Every eligible candidate failed connection admission: the same
            # deterministic pre-API exhaustion gate the router applies to an
            # empty pool — never an exception escaping switch_model.
            decision.why.setdefault("rejected", []).extend(rejects)
            decision.why["error"] = "NO_ELIGIBLE_MODEL"
            decision.why["connection_admission"] = {
                "stage": "candidate-admission", "reason": "connection_unresolved",
                "rejected": rejects, "routing_continued": False,
            }
            _safe_record(decision)
            raise ValueError(f"NO_ELIGIBLE_MODEL: {decision.why}")
        agent._fallback_chain = [chain_entry_for(route, existing, connections)
                                 for route in admitted if route != executed]
        agent._fallback_index = 0
        agent._routing_logical_route = workload.route
        # Only admitted routes may carry a mid-turn hop: an unresolved
        # candidate is skipped deterministically, not retried mid-turn.
        agent._routing_allowed_routes = set(admitted)
        if rejects:
            # Deterministic routing/failover reasons, visible in telemetry
            # (record_decision + turn_usage's durable row) and in the stashed
            # decision the rest of the turn consults.
            decision.why.setdefault("rejected", []).extend(rejects)
            decision.why["connection_admission"] = {
                "stage": "candidate-admission", "reason": "connection_unresolved",
                "rejected": rejects, "routing_continued": True,
                "selected": list(executed),
            }
            if executed != decision.route:
                from dataclasses import replace
                decision = replace(decision, provider=executed[0], model=executed[1])
                agent._routing_decision = decision
            _safe_record(decision)
        return
    chosen = ModelRouter().select_model(
        agent, {"vision": required.vision, "tool_use": required.tool_use},
        context_window=required.min_context, user_override=pinned,
    )
    if chosen is None:
        raise ValueError("Adaptive routing: no compatible configured route")
    if chosen != primary:
        # Use the same provider resolution and client lifecycle as an explicit
        # model switch; never construct a competing provider client here.
        # P1-1: the same two defensive layers apply on this seam — candidate
        # connection admission first, then the switch_model fail-safe — so an
        # unresolvable candidate can never crash the turn here either.
        legacy_decision = getattr(agent, "_routing_decision", None)
        if legacy_decision is not None and legacy_decision.route == chosen:
            legacy_eligible = [c.route for c in legacy_decision.ranked]
        else:
            # select_model returned an override without a fresh ranked pool;
            # only the chosen route itself is a known-fresh candidate.
            legacy_eligible = []
        legacy_eligible = legacy_eligible or [(chosen[0], chosen[1])]
        existing = {(str(e.get("provider") or "").strip().lower(),
                     str(e.get("model") or "").strip()): e
                    for e in agent._fallback_chain if isinstance(e, dict)}
        connections = connection_entries(config)
        admitted, admission_rejects = admit_candidate_connections(
            legacy_eligible, existing, connections,
            str(getattr(agent, "provider", "") or ""))
        executed = None
        guard_rejects = []
        if admitted:
            executed, guard_rejects = activate_admitted_candidate(
                agent, admitted, primary, existing, connections)
        rejects = admission_rejects + guard_rejects
        for row in rejects:
            _note_connection_rejection(agent, (row["route"][0], row["route"][1]))
        if executed is None:
            reason = {"stage": "candidate-admission",
                      "reason": "connection_unresolved",
                      "rejected": rejects, "routing_continued": False}
            detail = dict(getattr(legacy_decision, "why", {}) or {})
            detail["error"] = "NO_ELIGIBLE_MODEL"
            detail["connection_admission"] = reason
            raise ValueError(f"NO_ELIGIBLE_MODEL: {detail}")
        if rejects and legacy_decision is not None:
            legacy_decision.why.setdefault("rejected", []).extend(rejects)
            legacy_decision.why["connection_admission"] = {
                "stage": "candidate-admission", "reason": "connection_unresolved",
                "rejected": rejects, "routing_continued": True,
                "selected": list(executed),
            }
            if executed != legacy_decision.route:
                from dataclasses import replace
                legacy_decision = replace(legacy_decision,
                                          provider=executed[0], model=executed[1])
                agent._routing_decision = legacy_decision
            _safe_record(legacy_decision)


# -- connection resolution ------------------------------------------

def connection_entries(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map ``provider`` -> the configured connection (base_url + credential env).

    A registry row names ``provider``/``model_id``; the *transport* for that
    provider still lives in Hermes' own ``providers`` config. Without this,
    routes admitted from the registry but absent from the agent's pre-existing
    fallback chain would be switched to with an empty base_url and no key —
    i.e. a selection the runtime cannot actually execute.
    """
    try:
        from hermes_cli.config_providers import get_compatible_custom_providers

        entries: Dict[str, Dict[str, Any]] = {}
        for entry in get_compatible_custom_providers(config) or []:
            for name in (entry.get("provider_key"), entry.get("name")):
                key = str(name or "").strip().lower()
                if key and key not in entries:
                    entries[key] = entry
        return entries
    except Exception as exc:  # pragma: no cover - config shape is validated elsewhere
        _LOG.debug("connection_entries suppressed error: %s", exc)
        return {}


def chain_entry_for(route: Route, existing: Dict[Route, Dict[str, Any]],
                    connections: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Fallback-chain entry for ``route``: keep what the chain already had,
    otherwise take the transport from the provider's configured connection."""
    entry = dict(existing.get(route) or {})
    conn = connections.get(route[0])
    if conn:
        for source, target in (("base_url", "base_url"), ("api_mode", "api_mode"),
                               ("key_env", "key_env"), ("api_key_env", "key_env")):
            if not entry.get(target) and conn.get(source):
                entry[target] = conn[source]
    entry["provider"], entry["model"] = route[0], route[1]
    return entry


# -- P1-1: candidate connection admission (Layer A) ---------------------
#
# A routing candidate whose provider connection cannot be resolved used to
# survive candidate selection and reach ``switch_model``, which then raised
# ``ValueError: no base_url resolved ...`` and killed the whole turn
# (tui_gateway_crash.log:1534). Layer A filters such candidates BEFORE any
# switch is attempted; Layer B (``activate_admitted_candidate``) is the
# defense-in-depth guard around the switch itself.

#: Deterministic routing/failover reason for an unexecutable candidate. Reuses
#: the existing router ``rejected`` row shape (scoring.py) and the existing
#: failure classification (``FailoverReason.connection_unresolved``) — no
#: parallel failure framework.
CONNECTION_UNRESOLVED = "CONNECTION_UNRESOLVED"

# A base_url of exactly "scheme://" (authority missing entirely) can never be
# a usable endpoint for any client Hermes builds — the transport constructor
# rejects it before a request is ever attempted.
_EMPTY_AUTHORITY_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\s*$")


def _usable_base_url(value: Any) -> bool:
    """True when ``value`` is a structurally usable endpoint string.

    Admission mirrors ``switch_model``'s own resolution bar — a non-empty
    stripped string. The only extra structural rejection is a bare
    ``scheme://`` with no authority, which is unconditionally invalid. A
    present but wrong URL is deliberately NOT judged here: that is an API-time
    failure the existing retry/failover machinery already owns; admission must
    never convert unrelated provider errors into admission failures.
    """
    if not isinstance(value, str):
        return False
    base = value.strip()
    return bool(base) and not _EMPTY_AUTHORITY_URL.match(base)


def admit_candidate_connections(
    eligible: Sequence[Route],
    existing: Dict[Route, Dict[str, Any]],
    connections: Dict[str, Dict[str, Any]],
    current_provider: str,
) -> Tuple[List[Route], List[Dict[str, Any]]]:
    """Layer A — split the ranked eligible routes into admitted + rejected.

    A candidate is admitted when ``switch_model`` could actually activate it:

    * same-provider re-select keeps the established endpoint;
    * the candidate's chain entry carries a usable ``base_url``;
    * the provider has a configured connection with a usable ``base_url``;
    * the provider resolves its canonical endpoint itself (``openai``).

    Anything else — a missing provider connection, a missing/empty/blank
    ``base_url``, or a structurally unusable endpoint — is rejected with the
    deterministic ``CONNECTION_UNRESOLVED`` reason before provider execution:
    it cannot become the active model, cannot crash the turn, and routing
    continues with the next admitted candidate in rank order.
    """
    current = str(current_provider or "").strip().lower()
    admitted: List[Route] = []
    rejected: List[Dict[str, Any]] = []
    for raw_route in eligible:
        pair: Tuple[str, ...] = (tuple(raw_route) if isinstance(raw_route, (tuple, list))
                                 and len(raw_route) >= 2 else ("", ""))
        route: Route = (pair[0], pair[1])
        provider = str(route[0] or "").strip().lower()
        row = {"route": [route[0], route[1]], "reason": CONNECTION_UNRESOLVED,
               "stage": "connection_admission"}
        if not provider:
            rejected.append(row)
            continue
        if provider == current:
            admitted.append(route)
            continue
        entry = existing.get(route) or {}
        if _usable_base_url(entry.get("base_url")):
            admitted.append(route)
            continue
        conn = connections.get(provider) or {}
        if _usable_base_url(conn.get("base_url")):
            admitted.append(route)
            continue
        if provider == "openai":
            # switch_model resolves the canonical OpenAI endpoint itself
            # (agent_runtime_helpers._resolve_switch_destination).
            admitted.append(route)
            continue
        if _is_first_class_provider(provider):
            # First-class providers (lmstudio, nous, openai-codex, xai-oauth, ...)
            # resolve their own canonical endpoint via hermes_cli.auth /
            # runtime_provider — independent of the generic `providers:`
            # custom-connection config this function otherwise checks. A
            # registry.json route for one of these (e.g. a local LM Studio
            # model) has no matching `providers:` entry by design, so without
            # this it would be rejected as connection_unresolved even while
            # the local server is running and perfectly reachable (#lmstudio
            # registry/config mismatch).
            admitted.append(route)
            continue
        rejected.append(row)
    return admitted, rejected


def _is_first_class_provider(provider: str) -> bool:
    """True for a provider hermes_cli.auth can build a client for on its own
    (OAuth or well-known local runtime), with no ``providers:`` config entry."""
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        return provider in PROVIDER_REGISTRY
    except Exception:
        return False


def activate_admitted_candidate(
    agent: Any,
    admitted: Sequence[Route],
    primary: Optional[Route],
    existing: Dict[Route, Dict[str, Any]],
    connections: Dict[str, Dict[str, Any]],
) -> Tuple[Optional[Route], List[Dict[str, Any]]]:
    """Layer B — execute the first admitted candidate under the switch fail-safe.

    Walks the admitted candidates in rank order and switches through the
    canonical ``agent.switch_model`` lifecycle. A candidate whose switch
    raises :class:`SwitchEndpointUnresolvedError` — possible even after
    admission, e.g. a config edit racing the turn — is skipped, its reason
    recorded deterministically, and the walk continues with the next
    candidate: ``SKIP candidate -> record reason -> continue failover``. Any
    other exception propagates untouched: the guard must not hide programming
    errors, corrupt state or unrelated provider failures.

    Returns ``(executed_route, guard_rejects)``; ``executed_route`` is None
    when no candidate could be activated (the caller applies the existing
    deterministic ``NO_ELIGIBLE_MODEL`` exhaustion semantics).
    """
    from agent.agent_runtime_helpers import SwitchEndpointUnresolvedError
    from hermes_cli.fallback_config import resolve_entry_api_key

    executed: Optional[Route] = None
    guard_rejects: List[Dict[str, Any]] = []
    for route in admitted:
        if primary is not None and route == primary:
            executed = route  # already the live runtime; no switch needed
            break
        entry = chain_entry_for(route, existing, connections)
        try:
            agent.switch_model(route[1], route[0],
                               api_key=resolve_entry_api_key(entry) or "",
                               base_url=entry.get("base_url") or "",
                               api_mode=entry.get("api_mode") or "")
        except SwitchEndpointUnresolvedError as exc:
            guard_rejects.append({"route": [route[0], route[1]],
                                  "reason": CONNECTION_UNRESOLVED,
                                  "stage": "switch_model_guard", "detail": str(exc)})
            continue
        executed = route
        break
    return executed, guard_rejects


def _note_connection_rejection(agent: Any, route: Route,
                               *, registry: Optional[RouteRegistry] = None) -> None:
    """Record one connection-admission rejection through the existing node-D
    health + node-I telemetry seams (deterministic reason, provider/model
    labels only — never credentials). Best-effort: never raises, never breaks
    a turn."""
    try:
        from agent.error_classifier import FailoverReason
        from agent.routing import health as _health
        from agent.routing import telemetry as _tele

        reg = registry or getattr(getattr(agent, "_routing_runtime", None),
                                  "registry", None) or _default_registry
        reg.get(route[0], route[1])  # materialise so health can attach
        hd = _health.apply_failure(route[0], route[1],
                                   FailoverReason.connection_unresolved,
                                   registry=reg)
        _tele.record_health(hd)
    except Exception:  # pragma: no cover - telemetry must never break a turn
        _LOG.debug("connection admission rejection not recorded", exc_info=True)


# -- pool helpers -------------------------------------------------------

def build_pool_from_chain(
    primary: Optional[Tuple[str, str]],
    chain_entries: Sequence[Any],
) -> List[Route]:
    """``[primary] + fallback chain`` as normalised (provider, model) pairs.

    ``chain_entries`` items may be dicts (``{"provider","model"}``) or pairs.
    """
    pool: List[Route] = []
    seen: set[Route] = set()

    def _add(prov: Any, mdl: Any) -> None:
        if not prov or not mdl:
            return
        key = (str(prov).strip().lower(), str(mdl).strip())
        if key in seen:
            return
        seen.add(key)
        pool.append(key)

    if primary:
        _add(primary[0], primary[1])
    for e in chain_entries or []:
        if isinstance(e, dict):
            _add(e.get("provider"), e.get("model"))
        elif isinstance(e, (tuple, list)) and len(e) == 2:
            _add(e[0], e[1])
    return pool


def register_pool(pool: Sequence[Tuple[str, str]], *, registry: Optional[RouteRegistry] = None) -> None:
    reg = registry or _default_registry
    for prov, mdl in pool:
        try:
            reg.get(prov, mdl)  # materialise from models.dev if resolvable
        except Exception:  # pragma: no cover - defensive
            pass


# -- fallback-chain reorder (the live seam) --------------------------

def reorder_fallback_chain(agent: Any, reason: Any = None) -> Optional[Dict[str, Any]]:
    """Health-aware reorder of the unwalked fallback tail. No-op unless enabled."""
    if not adaptive_routing_enabled():
        return None
    try:
        chain = list(getattr(agent, "_fallback_chain", []) or [])
        idx = int(getattr(agent, "_fallback_index", 0) or 0)

        runtime = getattr(agent, "_routing_runtime", None)
        reg = runtime.registry if runtime else _default_registry
        hist = runtime.history if runtime else _default_history

        failed = (
            str(getattr(agent, "provider", "") or "").strip().lower(),
            str(getattr(agent, "model", "") or "").strip(),
        )
        from agent.error_classifier import FailoverReason
        from agent.routing import health as _health
        from agent.routing import telemetry as _tele

        if (failed[0] and failed[1] and isinstance(reason, FailoverReason)
                and getattr(agent, "_routing_failure_recorded", None) != failed):
            reg.get(*failed)
            hd = _health.apply_failure(failed[0], failed[1], reason, registry=reg)
            try:
                _tele.record_health(hd)
            except Exception:
                pass

        tail = chain[idx:]
        pool = build_pool_from_chain(None, tail)
        register_pool(pool, registry=reg)
        reg.resolve_expiries()  # eager sweep: readmit lapsed routes + clear stale raw health
        entries = reg.candidates(pool)
        req = getattr(agent, "_routing_required", RequiredCapabilities())
        ctx = getattr(agent, "_routing_context", None)
        ranked = rank_candidates(entries, req, history=hist, aggregate_history=True,
                                 logical_route=ctx.logical_route if ctx else None,
                                 zero_paid=bool(ctx and ctx.zero_paid))
        order = {c.route: i for i, c in enumerate(ranked.ranked)
                 if (c.entry.is_zero_cost if ctx and ctx.zero_paid else
                     c.entry.is_free or getattr(agent, "_routing_allow_paid", False))}
        agent._routing_allowed_routes = set(order)

        def _key(entry_dict: Any) -> Tuple[int, int]:
            if isinstance(entry_dict, dict):
                k = (str(entry_dict.get("provider", "")).strip().lower(),
                     str(entry_dict.get("model", "")).strip())
            else:
                k = ("", "")
            return (order.get(k, len(order) + 1), 0)

        new_tail = sorted(tail, key=_key)
        if new_tail == tail:
            return None
        agent._fallback_chain = chain[:idx] + new_tail
        payload = {
            "reordered_from": idx,
            "old": [f"{d.get('provider')}/{d.get('model')}" if isinstance(d, dict) else str(d) for d in tail],
            "new": [f"{d.get('provider')}/{d.get('model')}" if isinstance(d, dict) else str(d) for d in new_tail],
        }
        _LOG.info("routing.reorder %s", payload)
        return payload
    except Exception as exc:  # pragma: no cover - must never break a turn
        _LOG.debug("reorder_fallback_chain suppressed error: %s", exc)
        return None


def failover_restart_limit(agent: Any, default: int) -> int:
    """Per-turn bound on rebuilt-message restarts (turn_iteration_prep).

    The historical bound is ``api_max_retries`` (same-route retries), which also
    capped failover hops: with N healthy-looking candidates ahead of the first
    reachable one (e.g. a whole subscription account rate-limited) the turn
    died after ``default`` hops although eligible routes remained. Each
    failover restart is armed by one *successful* activation of an entry of the
    finite, router-built chain, so those hops are already bounded by the chain
    (``_fallback_index`` only advances). With adaptive routing on, the limit
    therefore grows by the activations spent this turn; non-failover restarts
    (truncation, ...) keep the original bound. Flag off: unchanged.
    """
    if not adaptive_routing_enabled():
        return default
    try:
        hops = int(getattr(agent, "_fallback_index", 0) or 0)
    except (TypeError, ValueError):
        return default
    return default + max(0, hops)


# -- richer facade (aux / graph seams, demo) ------------------------

def plan_route(
    *,
    candidate_routes: Sequence[Tuple[str, str]],
    required: Optional[RequiredCapabilities] = None,
    task_meta: Optional[Dict[str, Any]] = None,
    turn_override: Any = None,
    profile_default: Any = None,
    config_pin: Any = None,
    registry: Optional[RouteRegistry] = None,
    history: Optional[RoutingHistory] = None,
    record: bool = True,
) -> Optional[Decision]:
    """Full router pass. Returns None when the flag is off."""
    if not adaptive_routing_enabled():
        return None
    try:
        reg = registry or _default_registry
        hist = history if history is not None else _default_history
        pool = build_pool_from_chain(None, list(candidate_routes))
        register_pool(pool, registry=reg)
        req = required or classify_task(**(task_meta or {}))
        ov = resolve_override(turn_override=turn_override, profile_default=profile_default,
                              config_pin=config_pin, registry=reg)
        now_dt = datetime.now(timezone.utc)
        ctx = RouteContext(candidate_routes=pool, required=req, override=ov,
                           now=now_dt, now_epoch=now_dt.timestamp())
        decision = AdaptiveRouter(reg, hist).select(ctx)
        if record:
            _safe_record(decision)
        return decision
    except Exception as exc:  # pragma: no cover
        _LOG.debug("plan_route suppressed error: %s", exc)
        return None


def plan_recovery(
    *,
    candidate_routes: Sequence[Tuple[str, str]],
    failed_route: Tuple[str, str],
    classified_error: Any,
    tried: Sequence[Tuple[str, str]] = (),
    prior_hops: int = 0,
    required: Optional[RequiredCapabilities] = None,
    task_meta: Optional[Dict[str, Any]] = None,
    turn_override: Any = None,
    registry: Optional[RouteRegistry] = None,
    history: Optional[RoutingHistory] = None,
    record: bool = True,
) -> Optional[Decision]:
    if not adaptive_routing_enabled():
        return None
    try:
        reg = registry or _default_registry
        hist = history if history is not None else _default_history
        pool = build_pool_from_chain(None, list(candidate_routes))
        register_pool(pool, registry=reg)
        req = required or classify_task(**(task_meta or {}))
        ov = resolve_override(turn_override=turn_override, registry=reg)
        now_dt = datetime.now(timezone.utc)
        ctx = RouteContext(candidate_routes=pool, required=req, override=ov,
                           now=now_dt, now_epoch=now_dt.timestamp())
        decision = AdaptiveRouter(reg, hist).select_recovery(
            ctx, failed_route, classified_error, tried=tried, prior_hops=prior_hops,
        )
        if record and decision is not None:
            _safe_record(decision)
        return decision
    except Exception as exc:  # pragma: no cover
        _LOG.debug("plan_recovery suppressed error: %s", exc)
        return None


# -- node J write path -----------------------------------------------

def note_outcome(
    provider: str,
    model: str,
    ok: bool,
    *,
    cap_class: str = "",
    latency_ms: Optional[float] = None,
    session_id: Optional[str] = None,
    token_usage: Optional[Dict[str, int]] = None,
    reason: Any = None,
    registry: Optional[RouteRegistry] = None,
    history: Optional[RoutingHistory] = None,
) -> None:
    """Record the result of a routed call: history row + health update + telemetry.
    No-op when the flag is off."""
    if not adaptive_routing_enabled():
        return
    try:
        reg = registry or _default_registry
        hist = history if history is not None else _default_history
        from agent.error_classifier import ClassifiedError, FailoverReason
        from agent.routing import health as _health
        from agent.routing import telemetry as _tele

        reason_value = None
        if isinstance(reason, FailoverReason):
            reason_value = reason.value
        elif isinstance(reason, ClassifiedError):
            reason_value = reason.reason.value

        hist.record(provider, model, cap_class, ok, latency_ms=latency_ms, reason=reason_value)
        _tele._emit(_tele.RoutingDecisionRecord(
            ts=_tele._iso_now(), event="outcome", chosen=[provider, model],
            cap_class=cap_class, trigger=reason_value, session_id=session_id,
            latency_ms=latency_ms, token_usage=token_usage,
            outcome="success" if ok else "failure",
        ))

        if ok:
            _health.apply_success(provider, model, registry=reg)
        elif isinstance(reason, (FailoverReason, ClassifiedError)):
            hd = _health.apply_failure(provider, model, reason, registry=reg)
            try:
                _tele.record_health(hd)
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover
        _LOG.debug("note_outcome suppressed error: %s", exc)


def _safe_record(decision: Decision) -> None:
    try:
        from agent.routing import telemetry as _tele

        _tele.record_decision(decision)
    except Exception:  # pragma: no cover
        pass
