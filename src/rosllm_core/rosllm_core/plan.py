"""Plan parsing and full pre-execution validation.

A plan is newline-delimited JSON (NDJSON), one object per line:
``{"name": "<library action>", "input": {...}}``.

Every line is parsed and validated against the action library *before* the
executor performs any action, so a bad later step can never run earlier steps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .info import Info
from .jsonutil import StrictJSONError, dumps, loads
from .library import ActionLibrary, ActionSpec


class PlanError(ValueError):
    def __init__(self, message: str, info: Info = Info.INVALID_BEHAVIOR):
        super().__init__(message)
        self.info = info


@dataclass(frozen=True)
class PlanStep:
    index: int
    name: str
    spec: ActionSpec
    input_obj: dict
    input_text: str


@dataclass(frozen=True)
class Plan:
    steps: tuple[PlanStep, ...]
    raw: str


def parse_plan(
    behavior: Any,
    library: ActionLibrary,
    *,
    max_steps: int = 8,
    max_bytes: int = 16384,
    max_input_bytes: int = 4096,
) -> Plan:
    if not isinstance(behavior, str):
        raise PlanError("behavior must be a string")
    encoded = behavior.encode("utf-8")
    if len(encoded) > max_bytes:
        raise PlanError(
            f"behavior is {len(encoded)} bytes, limit is {max_bytes}",
            Info.LIMIT_EXCEEDED,
        )

    lines = [line for line in behavior.splitlines() if line.strip()]
    if not lines:
        raise PlanError("behavior is empty", Info.NULL_BEHAVIOR)
    if len(lines) > max_steps:
        raise PlanError(
            f"behavior has {len(lines)} steps, limit is {max_steps}",
            Info.LIMIT_EXCEEDED,
        )

    steps: list[PlanStep] = []
    for index, line in enumerate(lines):
        where = f"line {index + 1}"
        if len(line.encode("utf-8")) > max_input_bytes:
            raise PlanError(f"{where}: step is larger than {max_input_bytes} bytes")
        try:
            obj = loads(line)
        except StrictJSONError as exc:
            raise PlanError(f"{where}: {exc}") from None
        if not isinstance(obj, dict):
            raise PlanError(f"{where}: step must be a JSON object")
        unknown = sorted(set(obj) - {"name", "input"})
        if unknown:
            raise PlanError(f"{where}: unknown key(s) {unknown!r}")
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            raise PlanError(f"{where}: 'name' must be a non-empty string")
        if len(name) > 128:
            raise PlanError(f"{where}: 'name' is too long")
        spec = library.get(name)
        if spec is None:
            raise PlanError(
                f"{where}: action {name!r} is not in the allowed action library"
            )
        raw_input = obj.get("input", "")
        ok, normalized, error = spec.validate_input(raw_input)
        if not ok:
            raise PlanError(f"{where}: action {name!r}: {error}")
        steps.append(
            PlanStep(
                index=index,
                name=name,
                spec=spec,
                input_obj=normalized,
                input_text=dumps(normalized),
            )
        )
    return Plan(steps=tuple(steps), raw=behavior)


def to_ndjson(plan: Plan) -> str:
    return "\n".join(
        dumps({"name": step.name, "input": step.input_obj}) for step in plan.steps
    )
