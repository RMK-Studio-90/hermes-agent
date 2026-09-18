import json
with open(r'E:\KI\Hermes\routing\registry.json', encoding='utf-8') as f:
    doc = json.load(f)
from agent.routing.registry import RouteRegistry
reg = RouteRegistry()
reg.load_document(doc)
from agent.routing.router import AdaptiveRouter, RouteContext
from agent.routing.capabilities import RequiredCapabilities
from agent.routing.override import OverrideMode, resolve_override

req = RequiredCapabilities(vision=False, tool_use=True, reasoning=False, min_context=8000, long_output=False, structured_output=True)
router = AdaptiveRouter(reg)

# Test 1: Production error scenario (STRICT turn override with logical_route=rmk-code)
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
d = router.select(ctx)

# Test 2: Without turn override (routing profile active)
override_none = resolve_override(turn_override=None, registry=reg, default_mode=OverrideMode.SOFT)
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

# Test 3: Manual model selection (eligible model with STRICT)
override3 = resolve_override(
    turn_override=('omniroute', 'cc/claude-opus-4-7'),
    registry=reg,
    default_mode=OverrideMode.STRICT
)
ctx3 = RouteContext(
    candidate_routes=reg.known_routes(),
    required=req,
    logical_route='rmk-code',
    zero_paid=True,
    allow_paid=False,
    override=override3,
    now=1_700_000_000,
    now_epoch=1_700_000_000,
    half_life_seconds=3600,
)
d3 = router.select(ctx3)

# Test 4: No logical_route, STRICT override (should still work - original behavior)
override4 = resolve_override(
    turn_override=('nvidia', 'nvidia/nemotron-3-super-120b-a12b'),
    registry=reg,
    default_mode=OverrideMode.STRICT
)
ctx4 = RouteContext(
    candidate_routes=reg.known_routes(),
    required=req,
    logical_route=None,
    zero_paid=True,
    allow_paid=False,
    override=override4,
    now=1_700_000_000,
    now_epoch=1_700_000_000,
    half_life_seconds=3600,
)
d4 = router.select(ctx4)

results = {
    'test1_production_error': {
        'mode': d.mode,
        'route': [d.provider, d.model],
        'error': d.why.get("error", "none"),
        'override_why': d.why.get("override", "none"),
        'ranked': len(d.ranked) if d.ranked else 0
    },
    'test2_routing_profile_active': {
        'mode': d2.mode,
        'route': [d2.provider, d2.model],
        'error': d2.why.get("error", "none"),
        'ranked': len(d2.ranked) if d2.ranked else 0
    },
    'test3_manual_eligible_model': {
        'mode': d3.mode,
        'route': [d3.provider, d3.model],
        'error': d3.why.get("error", "none"),
        'override_why': d3.why.get("override", "none")
    },
    'test4_no_logical_route': {
        'mode': d4.mode,
        'route': [d4.provider, d4.model],
        'error': d4.why.get("error", "none"),
        'override_why': d4.why.get("override", "none")
    }
}

with open('all_test_results.json', 'w') as f:
    json.dump(results, f, indent=2)