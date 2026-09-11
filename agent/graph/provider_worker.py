"""One tool-free Hermes provider request in a killable process.

Uses existing provider/credential resolution. No memories, skills, plugins' tools,
background reviews, shell execution or delegated model loops are instantiated.
"""

import json
import sys


def complete(payload):
    from agent.auxiliary_client import get_text_auxiliary_client

    client, model = get_text_auxiliary_client("graph_" + payload["role"])
    if client is None or not model:
        raise ValueError("No configured Hermes model for graph role")
    # SDK transport retries are disabled where supported; HGES never retries a
    # failed provider request. Native provider adapters retain their own transport.
    if callable(getattr(client, "with_options", None)):
        client = client.with_options(max_retries=0, timeout=payload["timeout"])
    response = client.chat.completions.create(
        model=model, messages=payload["messages"], max_tokens=payload["max_tokens"])
    choice = response.choices[0]
    if getattr(choice.message, "tool_calls", None):
        raise ValueError("Graph roles cannot issue tool calls")
    content = choice.message.content
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Empty model result")
    usage = getattr(response, "usage", None)
    if usage is None:
        raise ValueError("Provider omitted usage; graph budget cannot be accounted")
    inputs = getattr(usage, "prompt_tokens", None)
    outputs = getattr(usage, "completion_tokens", None)
    if type(inputs) is not int or type(outputs) is not int or min(inputs, outputs) < 0:
        raise ValueError("Invalid provider usage")
    if getattr(choice, "finish_reason", None) in {"length", "max_tokens"}:
        raise ValueError("Model result exceeded output limit")
    cost = getattr(usage, "cost", None)
    return {"text": content, "model": model, "input_tokens": inputs, "output_tokens": outputs,
            "cost_usd": cost if type(cost) in (int, float) and cost >= 0 else None}


def main():
    try:
        result = complete(json.loads(sys.stdin.read()))
        print("HGES_RESULT=" + json.dumps(result))
    except Exception as exc:
        # Credentials and provider payloads must not be copied into persistent logs.
        print("HGES_ERROR=" + type(exc).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
