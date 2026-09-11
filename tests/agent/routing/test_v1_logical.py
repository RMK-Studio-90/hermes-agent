import pytest

from agent.routing.logical import classify_workload


@pytest.mark.parametrize("prompt,expected", [
    ("Help me organize my week", "rmk-general"),
    ("Implement a parser and unit test it", "rmk-code"),
    ("Compare architecture tradeoffs", "rmk-reason"),
    ("Research this subject using sources", "rmk-research"),
    ("Übersetze diesen Satz", "rmk-fast"),
])
def test_workload_rules(prompt, expected):
    assert classify_workload(prompt).route == expected


def test_explicit_and_visual_routes():
    assert classify_workload("Implement this", vision=True).route == "rmk-vision"
    assert classify_workload("hello", route="rmk-code").reason == "explicit-route"
    with pytest.raises(ValueError, match="Unknown logical route"):
        classify_workload(route="rmk-code/rmk-code")
