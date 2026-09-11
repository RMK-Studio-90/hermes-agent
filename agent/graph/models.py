"""Role calls through Hermes' configured auxiliary provider resolution."""

import json
import os
from pathlib import Path
import sys

from .process import run_process


class HermesModels:
    def __init__(self, request):
        self.request = request

    async def __call__(self, role, instruction, context):
        messages = [{"role": "system", "content": instruction + " Return one JSON object only."},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        # UTF-8 bytes deliberately overestimate normal text tokenization. Framing
        # reserve is explicit; provider-reported usage is checked before effects.
        if len(json.dumps(messages).encode()) + self.request.output_tokens + 1024 > self.request.node_tokens:
            raise ValueError("Role context exceeds node reservation; narrow task scope")
        payload = {"role": role, "messages": messages, "max_tokens": self.request.output_tokens,
                   "timeout": self.request.node_timeout}
        repo = Path(__file__).resolve().parents[2]
        environment = dict(os.environ)
        from hermes_constants import get_hermes_home
        environment["HERMES_HOME"] = str(get_hermes_home())
        environment["PYTHONPATH"] = str(repo)
        code, output = await run_process(
            [sys.executable, "-m", "agent.graph.provider_worker"], cwd=str(repo), env=environment,
            payload=json.dumps(payload).encode(), timeout=self.request.node_timeout)
        lines = [line[len("HGES_RESULT="):] for line in output.splitlines() if line.startswith("HGES_RESULT=")]
        if code or len(lines) != 1:
            raise ValueError("Hermes role request failed; no automatic provider retry")
        response = json.loads(lines[0])
        if response["input_tokens"] + response["output_tokens"] > self.request.node_tokens:
            raise ValueError("Provider usage exceeded reservation; refusing effects")
        parsed = json.loads(response.pop("text"))
        if not isinstance(parsed, dict):
            raise ValueError("Role output must be a JSON object")
        return parsed, response
