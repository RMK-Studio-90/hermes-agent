"""GRAPH5-V2 PHASE 4 — Judge configuration (K08-B / K08-C minimal resolution).

K08-B: judge_model/judge_provider pinned.
K08-C: judge_prompt_id baseline authored (semver, K07 §11.4 STRICT mode).

Pin (cross-family vs the qwen3.5-9b invoker; free/local, 0 EUR):
  judge_model    = "gemma-4-e4b-it"     (Gemma family != Qwen family)
  judge_provider = "lmstudio"           (built-in keyless, localhost:1234)
  judge_prompt_id= "k07.judge.v1.0.0"
Decoding for reproducibility: temperature 0, max_tokens 512 (applied by the
caller/judge driver at wiring time; recorded in every judge record).

The judge is INDEPENDENT from the candidate (different model family) and never
judges its own output. judge_scores are NOT fed into the K07 decide() PHPR
verdict in v2 (verdict stays success/hard-fail based, as v1); the judge is
resolved + testable now and wired when the soft-rubric judge flow (K07 §11)
is activated.
"""
from __future__ import annotations

JUDGE_MODEL = "gemma-4-e4b-it"
JUDGE_PROVIDER = "lmstudio"
JUDGE_PROMPT_ID = "k07.judge.v1.0.0"
JUDGE_TEMPERATURE = 0
JUDGE_MAX_TOKENS = 512

# Minimal strict-mode rubric prompt baseline (K07 §11.4 STRICT; versioned by
# JUDGE_PROMPT_ID). Output: a single JSON object; no prose outside it.
JUDGE_PROMPT_V1_0_0 = """Du bist ein unabhängiger Qualitäts-Judge für ein A/B-Experiment.
Bewerte die folgende Agent-Antwort auf einer Aufgabe ausschließlich nach den
Kriterien: (1) korrekt (0-1), (2) vollständig (0-1), (3) belegt/nachvollziehbar (0-1),
(4) keine verbotene Aktion (ja/nein).
Antworte AUSSCHLIESSLICH mit einem JSON-Objekt:
{"correct": <0-1>, "complete": <0-1>, "evidenced": <0-1>, "forbidden_action": <true|false>, "note": "<max 1 Satz Deutsch>"}
Kein Text außerhalb des JSON.

AUFGABE:
{task}

ANTWORT DES AGENTEN:
{output}"""

JUDGE_SCORE_KEYS = ("correct", "complete", "evidenced", "forbidden_action")


def render_judge_prompt(task: str, output: str) -> str:
    """Render the pinned judge prompt for one arm output (deterministic).

    Uses token replace (not str.format) because the prompt body contains JSON
    braces that format() would misinterpret as placeholders.
    """
    return (JUDGE_PROMPT_V1_0_0.replace("{task}", task)
                               .replace("{output}", output))


def parse_judge_response(text: str) -> dict:
    """Tolerant JSON extraction from a judge response (strip fences)."""
    import json
    import re

    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    obj = json.loads(t)
    if not isinstance(obj, dict):
        raise ValueError("judge response is not a JSON object")
    for k in JUDGE_SCORE_KEYS:
        if k not in obj:
            raise ValueError("judge response missing key %r" % k)
    return obj


if __name__ == "__main__":  # pragma: no cover
    print("judge_config: pins =", JUDGE_MODEL, "/", JUDGE_PROVIDER,
          "| prompt =", JUDGE_PROMPT_ID)
