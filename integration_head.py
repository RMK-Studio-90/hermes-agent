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
    primary = (agent.provider, agent.model)
    # Existing conversations retain their provider/prompt prefix. Explicit model
    # switches already use the canonical switch_model API and remain authoritative.
    pinned = (getattr(agent, "_routing_explicit_model", True)
              or bool(conversation_history) or bool(getattr(agent, "_cached_system_prompt", None)))
    from agent.routing.runtime import configured_router
    runtime = configured_router(config)
    if runtime is not None:
        from agent.routing.logical import classify_workload
        logical = getattr(agent, "_routing_logical_route", None) or adaptive.get("route")
        workload = classify_workload(str(user_message or ""), route=logical, vision=has_images)
        decision = ModelRouter().select_workload(
            agent, runtime, required, workload, pinned=primary if pinned else None)
        if decision.route is None:
            raise ValueError(f"NO_ELIGIBLE_MODEL: {decision.why}")
        # Execution keeps Hermes' canonical provider lifecycle. Only the pool
        # and order come from Smart Routing; no gateway combo is introduced.
        existing = {(str(e.get("provider") or "").strip().lower(),
                     str(e.get("model") or "").strip()): e
                    for e in agent._fallback_chain if isinstance(e, dict)}
        connections = connection_entries(config)
        # Only the *eligible* candidates for this route become the fallback pool,
        # so a mid-turn hop can never land on a model this route excluded.
        eligible = [c.route for c in decision.ranked] or [decision.route]
        agent._fallback_chain = [chain_entry_for(route, existing, connections)
                                 for route in eligible if route != decision.route]
        agent._fallback_index = 0
        agent._routing_logical_route = workload.route
        agent._routing_allowed_routes = set(eligible)
        if decision.route != primary:
            entry = chain_entry_for(decision.route, existing, connections)
            from hermes_cli.fallback_config import resolve_entry_api_key
            agent.switch_model(decision.model, decision.provider,
                               api_key=resolve_entry_api_key(entry) or "",
                               base_url=entry.get("base_url") or "",
                               api_mode=entry.get("api_mode") or "")
        return
    chosen = ModelRouter().select_model(
        agent, {"vision": required.vision, "tool_use": required.tool_use},
        context_window=required.min_context, user_override=primary if pinned else None,
    )
    if chosen is None:
        raise ValueError("Adaptive routing: no compatible configured route")
    if chosen != primary:
        # Use the same provider resolution and client lifecycle as an explicit
        # model switch; never construct a competing provider client here.
        entry = next(e for e in agent._fallback_chain
                     if (e.get("provider"), e.get("model")) == chosen)
        from hermes_cli.fallback_config import resolve_entry_api_key
        agent.switch_model(chosen[1], chosen[0],
                           api_key=resolve_entry_api_key(entry) or "",
                           base_url=entry.get("base_url") or "",
                           api_mode=entry.get("api_mode") or "")


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
