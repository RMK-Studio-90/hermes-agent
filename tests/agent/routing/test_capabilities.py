"""Node C unit tests — task capability classification."""

from __future__ import annotations

from agent.routing.capabilities import RequiredCapabilities, cap_class, classify_task


def test_plain_text_turn_needs_nothing_special() -> None:
    r = classify_task(prompt="hello there")
    assert r.vision is False
    assert r.tool_use is False
    assert r.reasoning is False
    assert r.long_output is False
    assert r.min_context == 8_000


def test_image_attachment_requires_vision() -> None:
    assert classify_task(attachments=["image/png"]).vision is True
    assert classify_task(attachments=["holiday.JPG"]).vision is True
    assert classify_task(attachments=["application/pdf"]).vision is True
    assert classify_task(has_images=True).vision is True
    assert classify_task(attachments=["text/plain"]).vision is False


def test_tools_present_requires_tool_use() -> None:
    assert classify_task(tools=["bash", "read"]).tool_use is True
    assert classify_task(tools=[]).tool_use is False
    assert classify_task(force_tools=True).tool_use is True
    assert classify_task(tools=[None, ""]).tool_use is False


def test_reasoning_from_task_type_and_complexity() -> None:
    assert classify_task(task_type="software_change").reasoning is True
    assert classify_task(task_type="analysis").reasoning is True
    assert classify_task(task_type="text").reasoning is False
    assert classify_task(complexity=6).reasoning is True
    assert classify_task(complexity=3).reasoning is False
    # explicit override wins
    assert classify_task(task_type="software_change", needs_reasoning=False).reasoning is False
    assert classify_task(task_type="text", needs_reasoning=True).reasoning is True


def test_long_output_detection() -> None:
    assert classify_task(task_type="software_change").long_output is True
    assert classify_task(task_type="text").long_output is False
    assert classify_task(task_type="text", expects_long_output=True).long_output is True


def test_context_estimate_buckets_up() -> None:
    # tiny prompt -> smallest bucket
    assert classify_task(prompt="x" * 10).min_context == 8_000
    # ~30k tokens of history -> next bucket above 30k+overhead
    r = classify_task(history_chars=120_000)  # ~30k tokens + 4k overhead = 34k
    assert r.min_context == 64_000
    # explicit context_tokens honoured and bucketed
    assert classify_task(context_tokens=150_000).min_context == 200_000
    assert classify_task(context_tokens=5_000_000).min_context >= 5_000_000


def test_cap_class_is_stable_and_readable() -> None:
    r = RequiredCapabilities(vision=True, tool_use=True, reasoning=True, long_output=True,
                             min_context=200_000)
    assert r.cap_class() == "vision+tools+reasoning+long+ctx200k"
    assert cap_class(r) == r.cap_class()

    r2 = RequiredCapabilities(min_context=8_000)
    assert r2.cap_class() == "ctx8k"

    r3 = RequiredCapabilities(tool_use=True, min_context=128_000)
    assert r3.cap_class() == "tools+ctx128k"

    # 1M window label
    assert RequiredCapabilities(min_context=1_000_000).cap_class() == "ctx1m"


def test_deterministic() -> None:
    kw = dict(prompt="analyse this", history_chars=5000, attachments=["image/png"],
              tools=["bash"], task_type="analysis", complexity=7)
    assert classify_task(**kw) == classify_task(**kw)


def test_frozen_dataclass_is_hashable() -> None:
    r = classify_task(tools=["bash"])
    d = {r: 1}
    assert d[r] == 1
