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
router = AdaptiveRouter(reg)
d = router.select(ctx)

with open('test_results.json', 'w') as f:
    json.dump({
        'test1': {
            'mode': d.mode,
            'route': [d.provider, d.model],
            'error': d.why.get("error", "none"),
            'override_why': d.why.get("override", "none"),
            'ranked': len(d.ranked) if d.ranked else 0
        }
    }, f, indent=2)