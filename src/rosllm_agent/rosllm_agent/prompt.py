"""Upstream-compatible prompt construction and conservative plan extraction."""

from __future__ import annotations

import re

PROMPT_TEMPLATE = """You are an agent that selects appropriate actions based on human input and observations from the environment.

# Atomic actions
You have several atomic actions available to you, which are defined in the atomic action library (split into name and description).
{atomic_action_library}

{expert_advice}

# Observations
This is a list of observations from the environment, you should use this to select an appropriate action.
{observations}

# Human inputs
This is a list of human inputs, you should use this to select an appropriate action.
{human_inputs}

{response_format}
"""

RESPONSE_FORMAT = """# Response format
Your response should be formatted as follows (ie json lines):
```json
{"name": "...", "input": {...}}
{"name": "...", "input": {...}}
```
where "name" is the name of an atomic action from the library above, and "input" is a JSON object matching that action's input schema. Use an empty object {{}} when the action takes no input.
You may select a sequence of actions or a single action, based on the environment state and the human input.
Do not invent action names or parameters, and do not output anything outside the fenced json block."""

EXPERT_ADVICE_TEMPLATE = """# Expert advice
{expert_advice}"""

MAX_PLAN_TEXT_BYTES = 65536

_FENCE_RE = re.compile(
    r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL
)


def build_prompt(
    atomic_action_library: str,
    observations: str,
    human_inputs: str,
    expert_advice: str = "",
) -> str:
    advice = (
        EXPERT_ADVICE_TEMPLATE.format(expert_advice=expert_advice.strip())
        if expert_advice and expert_advice.strip()
        else ""
    )
    return PROMPT_TEMPLATE.format(
        atomic_action_library=atomic_action_library,
        observations=observations,
        human_inputs=human_inputs,
        expert_advice=advice,
        response_format=RESPONSE_FORMAT,
    )


def extract_plan_text(response: str) -> str:
    """Return the plan text from a model response.

    Uses all fenced ``json``/``jsonl`` blocks (or unlabelled fences) when
    present; otherwise the whole response is returned and later strict-parsed.
    Other fenced languages (for example a Python explanation) are ignored so
    that prose cannot be mistaken for a plan.
    """
    if not isinstance(response, str):
        return ""
    blocks = [
        match.group(2)
        for match in _FENCE_RE.finditer(response)
        if match.group(1).lower() in ("", "json", "jsonl")
    ]
    text = "\n".join(block.strip() for block in blocks) if blocks else response
    text = text.strip()
    if len(text.encode("utf-8")) > MAX_PLAN_TEXT_BYTES:
        text = text[:MAX_PLAN_TEXT_BYTES]
    return text
