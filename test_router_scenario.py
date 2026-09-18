import json
from pathlib import Path

with open(r'E:\KI\Hermes\routing\registry.json', encoding='utf-8') as f:
    doc = json.load(f)

from agent.routing.registry import RouteRegistry
reg = RouteRegistry()
reg.load_document(doc)

from agent.routing.router import AdaptiveRouter, RouteContext
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.override import OverrideMode, resolve_override

req = RequiredCapabilities(vision=False, tool_use=True, reasoning=False, min_context=8000, long_output=False, structured_output=True)

override = resolve_override(
    turn_override=('nvidia', 'nvidia/nemotron-3-super-120b-a12b'),
    registry=reg,
    default_mode=OverrideMode.STRICT
)

ctx = RouteContext(
    candidate_routes=reg.known_routes(),
    required=req,
    logical_route='rmk-code',
    zero_paid=True,
    allow_paid=False,
    override=override,
    now=1_700_000_000,
    now_epoch=1_700_000_000,
    half_life_seconds=3600,
)

router = AdaptiveRouter(reg)
d = router.select(ctx)

print("mode:", d.mode)
print("route:", d.provider, d.model)
print("error:", d.why.get('error') if d.why else 'none')
print("rejected:", d.why.get('rejected') if d.why else 'none')
print("ranked:", len(d.ranked) if d.ranked else 0)

# Now test WITHOUT the turn override (simulating routing-profile active)
override_none = resolve_override(
    turn_override=None,
    registry=reg,
    default_mode=OverrideMode.SOFT
)

ctx2 = RouteContext(
    candidate_routes=reg.known_routes(),
    required=req,
    logical_route='rmk-code',
    zero_paid=True,
    allow_paid=False,
    override=override_none,
    now=1_700_000_000,
    now_epoch=1_700_000_000,
    half_life_seconds=3600,
)

d2 = router.select(ctx2)
print()
print("--- Without turn override (routing-profile active) ---")
print("mode:", d2.mode)
print("route:", d2.provider, d2.model)
print("error:", d2.why.get('error') if d2.why else 'none')
print("rejected:", d2.why.get('rejected') if d2.why else 'none')
print("ranked:", len(d2.ranked) if d2.ranked else 0)
if d2.ranked:
    for c in d2.ranked[:10]:
        print("  ", c.route, "score:", round(c.score, 3))