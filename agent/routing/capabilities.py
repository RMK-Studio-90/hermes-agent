"""Node C — task capability classification.

Maps the metadata of one turn/task to the set of capabilities a model MUST have
to serve it. Pure and deterministic: same inputs -> same output, no network, no
config reads. The result feeds the hard capability filter in ``scoring.py`` and
supplies a stable ``cap_class`` string used as a bucket key by the adaptive
history store (``history.py``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# Context-window buckets (tokens). A task's estimated need is rounded UP to the
# nearest bucket so small estimation noise does not disqualify a model.
_CONTEXT_BUCKETS = (8_000, 16_000, 32_000, 64_000, 128_000, 200_000, 400_000, 1_000_000)

# task_type values (HGES + chat) that imply multi-step reasoning.
_REASONING_TASK_TYPES = frozenset(
    {"software_change", "analysis", "reasoning", "planning", "research", "debug"}
)
_LONG_OUTPUT_TASK_TYPES = frozenset({"software_change", "code_generation", "long_form"})

_IMAGE_HINTS = ("image/", "img", "photo", "screenshot", "png", "jpg", "jpeg", "webp", "gif")
_DOC_HINTS = ("application/pdf", "pdf")


@dataclass(frozen=True)
class RequiredCapabilities:
    """What a model must support to serve a given task."""

    vision: bool = False
    tool_use: bool = False
    reasoning: bool = False
    min_context: int = 8_000
    long_output: bool = False
    structured_output: bool = False

    def cap_class(self) -> str:
        """Stable, low-cardinality bucket key for the history store.

        e.g. ``"ctx32k"``, ``"tools+ctx128k"``, ``"vision+tools+reasoning+long+ctx200k"``.
        """
        parts = []
        if self.vision:
            parts.append("vision")
        if self.tool_use:
            parts.append("tools")
        if self.reasoning:
            parts.append("reasoning")
        if self.long_output:
            parts.append("long")
        if self.structured_output:
            parts.append("structured")
        parts.append(f"ctx{_ctx_label(self.min_context)}")
        return "+".join(parts)


def _ctx_label(tokens: int) -> str:
    if tokens >= 1_000_000:
        return "1m"
    if tokens >= 1_000:
        return f"{tokens // 1000}k"
    return str(tokens)


def _bucket_up(tokens: int) -> int:
    for b in _CONTEXT_BUCKETS:
        if tokens <= b:
            return b
    return tokens


def _looks_visual(mimetype_or_name: str) -> bool:
    low = mimetype_or_name.lower()
    return any(h in low for h in _IMAGE_HINTS) or any(h in low for h in _DOC_HINTS)


def _estimate_tokens(text_len_chars: int, history_chars: int, attachment_count: int) -> int:
    # ~4 chars/token is the standard rough heuristic; add headroom for the
    # system prompt, tool schemas and the model's own reply.
    base = math.ceil((text_len_chars + history_chars) / 4)
    overhead = 4_000 + attachment_count * 1_500
    return base + overhead


def classify_task(
    *,
    prompt: str = "",
    history_chars: int = 0,
    attachments: Optional[Sequence[str]] = None,
    has_images: bool = False,
    tools: Optional[Iterable[str]] = None,
    force_tools: bool = False,
    task_type: Optional[str] = None,
    complexity: Optional[int] = None,
    needs_reasoning: Optional[bool] = None,
    expects_long_output: Optional[bool] = None,
    context_tokens: Optional[int] = None,
) -> RequiredCapabilities:
    """Classify one turn.

    Args:
        prompt: the user message text (only its length is used).
        history_chars: total characters of conversation history to be sent.
        attachments: mimetypes or filenames of attached parts.
        has_images: explicit signal that image parts are present.
        tools: names of tools exposed for this turn.
        force_tools: caller knows tool use is mandatory regardless of ``tools``.
        task_type: HGES/chat task category.
        complexity: HGES 1-10 complexity, if known.
        needs_reasoning: explicit override for the reasoning requirement.
        expects_long_output: explicit override for the long-output requirement.
        context_tokens: exact token budget if the caller already computed it.
    """
    attachments = list(attachments or [])
    tool_list = [t for t in (tools or []) if t]

    vision = bool(has_images) or any(_looks_visual(a) for a in attachments)
    tool_use = force_tools or len(tool_list) > 0

    if needs_reasoning is not None:
        reasoning = bool(needs_reasoning)
    else:
        reasoning = (task_type in _REASONING_TASK_TYPES) or (
            complexity is not None and complexity >= 6
        )

    if expects_long_output is not None:
        long_output = bool(expects_long_output)
    else:
        long_output = task_type in _LONG_OUTPUT_TASK_TYPES

    if context_tokens is not None and context_tokens > 0:
        est = int(context_tokens)
    else:
        est = _estimate_tokens(len(prompt or ""), max(0, history_chars), len(attachments))
    min_context = _bucket_up(est)

    return RequiredCapabilities(
        vision=vision,
        tool_use=tool_use,
        reasoning=reasoning,
        min_context=min_context,
        long_output=long_output,
    )


def cap_class(required: RequiredCapabilities) -> str:
    """Module-level convenience wrapper for :meth:`RequiredCapabilities.cap_class`."""
    return required.cap_class()
