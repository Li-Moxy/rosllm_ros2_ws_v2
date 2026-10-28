"""Atomic-action library: the single source of truth for prompt text, the
executor whitelist, input validation and post-condition verification.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .jsonutil import StrictJSONError, dumps, loads

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_SERVICE_RE = re.compile(r"^/?[A-Za-z][A-Za-z0-9_]*(/[A-Za-z][A-Za-z0-9_]*)*$")
_FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z_][A-Za-z0-9_-]*)*$")

_ALLOWED_TYPES = ("number", "integer", "string", "boolean")
_ALLOWED_DIRECTIONS = ("any", "increase", "decrease")


class LibraryError(ValueError):
    """Invalid or unreadable action library."""


@dataclass(frozen=True)
class ParamSpec:
    name: str
    type: str
    required: bool = True
    minimum: float | None = None
    maximum: float | None = None
    max_length: int | None = None
    enum: tuple[Any, ...] | None = None
    description: str = ""

    def schema(self) -> dict:
        out: dict[str, Any] = {"type": self.type}
        if self.minimum is not None:
            out["min"] = self.minimum
        if self.maximum is not None:
            out["max"] = self.maximum
        if self.max_length is not None:
            out["max_length"] = self.max_length
        if self.enum is not None:
            out["enum"] = list(self.enum)
        if self.description:
            out["description"] = self.description
        return out

    def validate(self, value: Any) -> tuple[bool, str]:
        """Return ``(ok, error)``.  Never raises for user-supplied values."""
        if self.type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, f"parameter {self.name!r} must be a number"
            number = float(value)
            if not math.isfinite(number):
                return False, f"parameter {self.name!r} must be finite"
            if self.minimum is not None and number < self.minimum:
                return False, f"parameter {self.name!r} < min {self.minimum}"
            if self.maximum is not None and number > self.maximum:
                return False, f"parameter {self.name!r} > max {self.maximum}"
        elif self.type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                return False, f"parameter {self.name!r} must be an integer"
            if self.minimum is not None and value < self.minimum:
                return False, f"parameter {self.name!r} < min {int(self.minimum)}"
            if self.maximum is not None and value > self.maximum:
                return False, f"parameter {self.name!r} > max {int(self.maximum)}"
        elif self.type == "string":
            if not isinstance(value, str):
                return False, f"parameter {self.name!r} must be a string"
            if self.max_length is not None and len(value) > self.max_length:
                return False, f"parameter {self.name!r} is longer than {self.max_length}"
            if "\x00" in value:
                return False, f"parameter {self.name!r} must not contain NUL"
        elif self.type == "boolean":
            if not isinstance(value, bool):
                return False, f"parameter {self.name!r} must be a boolean"
        else:  # pragma: no cover - guarded at load time
            return False, f"parameter {self.name!r} has unsupported type {self.type!r}"

        if self.enum is not None and value not in self.enum:
            return False, f"parameter {self.name!r} is not one of {list(self.enum)!r}"
        return True, ""


@dataclass(frozen=True)
class VerifySpec:
    observation: str
    field: str
    min_abs_delta: float = 1e-6
    direction: str = "any"
    metric: str = "delta"


@dataclass(frozen=True)
class ActionSpec:
    name: str
    service: str
    description: str
    parameters: tuple[ParamSpec, ...] = ()
    verify: VerifySpec | None = None

    @property
    def parameter_map(self) -> dict[str, ParamSpec]:
        return {p.name: p for p in self.parameters}

    def validate_input(self, raw: Any) -> tuple[bool, dict, str]:
        """Validate a plan ``input`` value.

        ``raw`` may be a dict, a JSON-object string, or ``None``/``""`` for an
        action without parameters.  Returns ``(ok, normalized_object, error)``.
        """
        if raw is None:
            raw = ""
        if isinstance(raw, str):
            if raw.strip() == "":
                obj: dict = {}
            else:
                try:
                    obj = loads(raw)
                except StrictJSONError as exc:
                    return False, {}, f"input is not strict JSON: {exc}"
        elif isinstance(raw, dict):
            obj = dict(raw)
        else:
            return False, {}, "input must be a JSON object or an object string"
        if not isinstance(obj, dict):
            return False, {}, "input must be a JSON object"

        params = self.parameter_map
        unknown = sorted(set(obj) - set(params))
        if unknown:
            return False, {}, f"unknown input parameter(s) {unknown!r}"
        for spec in self.parameters:
            if spec.name not in obj:
                if spec.required:
                    return False, {}, f"missing required input parameter {spec.name!r}"
                continue
            ok, error = spec.validate(obj[spec.name])
            if not ok:
                return False, {}, error
        return True, obj, ""


@dataclass
class ActionLibrary:
    path: Path
    actions: dict[str, ActionSpec] = field(default_factory=dict)
    version: int = 1

    # ------------------------------------------------------------------ load
    @classmethod
    def load(
        cls,
        path: str | Path,
        max_actions: int = 64,
        max_description_bytes: int = 16384,
    ) -> "ActionLibrary":
        source = Path(path).expanduser()
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise LibraryError(f"cannot read action library {source}: {exc}") from exc
        if len(text.encode("utf-8")) > 512 * 1024:
            raise LibraryError("action library file is larger than 512 KiB")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise LibraryError(f"invalid YAML in {source}: {exc}") from exc
        if not isinstance(data, dict):
            raise LibraryError("action library root must be a mapping")
        unknown = sorted(set(data) - {"version", "actions"})
        if unknown:
            raise LibraryError(f"unknown top-level key(s) {unknown!r}")
        version = data.get("version")
        if version != 1 or isinstance(version, bool):
            raise LibraryError("action library 'version' must be the integer 1")
        raw_actions = data.get("actions")
        if not isinstance(raw_actions, dict) or not raw_actions:
            raise LibraryError("'actions' must be a non-empty mapping")
        if len(raw_actions) > max_actions:
            raise LibraryError(f"too many actions ({len(raw_actions)} > {max_actions})")

        actions: dict[str, ActionSpec] = {}
        for name, entry in raw_actions.items():
            if not isinstance(name, str) or not _NAME_RE.match(name):
                raise LibraryError(f"invalid action name {name!r}")
            actions[name] = cls._load_action(
                source, name, entry, max_description_bytes
            )
        return cls(path=source, actions=actions, version=1)

    @classmethod
    def _load_action(
        cls,
        library_path: Path,
        name: str,
        entry: Any,
        max_description_bytes: int,
    ) -> ActionSpec:
        if not isinstance(entry, dict):
            raise LibraryError(f"action {name!r} must be a mapping")
        unknown = sorted(
            set(entry) - {"service", "description", "description_file", "parameters", "verify"}
        )
        if unknown:
            raise LibraryError(f"action {name!r} has unknown key(s) {unknown!r}")
        service = entry.get("service")
        if not isinstance(service, str) or not _SERVICE_RE.match(service):
            raise LibraryError(f"action {name!r} needs a valid ROS service name")
        if len(service) > 256:
            raise LibraryError(f"action {name!r} service name is too long")

        inline = entry.get("description")
        description_file = entry.get("description_file")
        if (inline is None) == (description_file is None):
            raise LibraryError(
                f"action {name!r} needs exactly one of 'description' or 'description_file'"
            )
        if description_file is not None:
            if not isinstance(description_file, str) or not description_file:
                raise LibraryError(f"action {name!r} description_file must be a string")
            target = (library_path.parent / description_file).resolve()
            try:
                description = target.read_text(encoding="utf-8")
            except OSError as exc:
                raise LibraryError(
                    f"action {name!r} description_file unreadable: {exc}"
                ) from exc
        else:
            description = inline
        if not isinstance(description, str) or not description.strip():
            raise LibraryError(f"action {name!r} description must be a non-empty string")
        if len(description.encode("utf-8")) > max_description_bytes:
            raise LibraryError(
                f"action {name!r} description exceeds {max_description_bytes} bytes"
            )

        raw_params = entry.get("parameters", {})
        if not isinstance(raw_params, dict):
            raise LibraryError(f"action {name!r} 'parameters' must be a mapping")
        if len(raw_params) > 16:
            raise LibraryError(f"action {name!r} has more than 16 parameters")
        parameters = tuple(
            cls._load_param(name, pname, pspec)
            for pname, pspec in raw_params.items()
        )

        raw_verify = entry.get("verify")
        verify = None
        if raw_verify is not None:
            verify = cls._load_verify(name, raw_verify)

        return ActionSpec(
            name=name,
            service=service,
            description=description.strip(),
            parameters=parameters,
            verify=verify,
        )

    @classmethod
    def _load_param(cls, action: str, pname: Any, spec: Any) -> ParamSpec:
        if not isinstance(pname, str) or not _PARAM_RE.match(pname):
            raise LibraryError(f"action {action!r}: invalid parameter name {pname!r}")
        if not isinstance(spec, dict):
            raise LibraryError(f"action {action!r} parameter {pname!r} must be a mapping")
        unknown = sorted(
            set(spec) - {"type", "required", "min", "max", "max_length", "enum", "description"}
        )
        if unknown:
            raise LibraryError(
                f"action {action!r} parameter {pname!r} has unknown key(s) {unknown!r}"
            )
        ptype = spec.get("type")
        if ptype not in _ALLOWED_TYPES:
            raise LibraryError(
                f"action {action!r} parameter {pname!r} type must be one of {_ALLOWED_TYPES}"
            )
        required = spec.get("required", True)
        if not isinstance(required, bool):
            raise LibraryError(f"action {action!r} parameter {pname!r} required must be bool")

        minimum = spec.get("min")
        maximum = spec.get("max")
        if ptype in ("number", "integer"):
            for label, value in (("min", minimum), ("max", maximum)):
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise LibraryError(
                            f"action {action!r} parameter {pname!r} {label} must be numeric"
                        )
                    if not math.isfinite(float(value)):
                        raise LibraryError(
                            f"action {action!r} parameter {pname!r} {label} must be finite"
                        )
            if minimum is not None and maximum is not None:
                if float(minimum) > float(maximum):
                    raise LibraryError(
                        f"action {action!r} parameter {pname!r} min > max"
                    )
            if ptype == "integer":
                for label, value in (("min", minimum), ("max", maximum)):
                    if value is not None and float(value) != int(value):
                        raise LibraryError(
                            f"action {action!r} parameter {pname!r} {label} must be an integer"
                        )
        else:
            if minimum is not None or maximum is not None:
                raise LibraryError(
                    f"action {action!r} parameter {pname!r}: min/max only apply to numbers"
                )

        max_length = spec.get("max_length")
        if max_length is not None:
            if ptype != "string":
                raise LibraryError(
                    f"action {action!r} parameter {pname!r}: max_length only applies to strings"
                )
            if isinstance(max_length, bool) or not isinstance(max_length, int):
                raise LibraryError(
                    f"action {action!r} parameter {pname!r} max_length must be an integer"
                )
            if not 1 <= max_length <= 4096:
                raise LibraryError(
                    f"action {action!r} parameter {pname!r} max_length outside 1..4096"
                )

        enum = spec.get("enum")
        if enum is not None:
            if not isinstance(enum, list) or not 1 <= len(enum) <= 32:
                raise LibraryError(
                    f"action {action!r} parameter {pname!r} enum must be a 1..32 item list"
                )
            for value in enum:
                if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                    raise LibraryError(
                        f"action {action!r} parameter {pname!r} enum values must be scalars"
                    )
                if isinstance(value, float) and not math.isfinite(value):
                    raise LibraryError(
                        f"action {action!r} parameter {pname!r} enum must be finite"
                    )
            enum = tuple(enum)

        description = spec.get("description", "")
        if not isinstance(description, str):
            raise LibraryError(
                f"action {action!r} parameter {pname!r} description must be a string"
            )
        return ParamSpec(
            name=pname,
            type=ptype,
            required=required,
            minimum=None if minimum is None else float(minimum),
            maximum=None if maximum is None else float(maximum),
            max_length=max_length,
            enum=enum,
            description=description,
        )

    @classmethod
    def _load_verify(cls, action: str, spec: Any) -> VerifySpec:
        if not isinstance(spec, dict):
            raise LibraryError(f"action {action!r} 'verify' must be a mapping")
        unknown = sorted(
            set(spec) - {"observation", "field", "min_abs_delta", "direction", "metric"}
        )
        if unknown:
            raise LibraryError(f"action {action!r} verify has unknown key(s) {unknown!r}")
        observation = spec.get("observation")
        if not isinstance(observation, str) or not _NAME_RE.match(observation):
            raise LibraryError(f"action {action!r} verify.observation is invalid")
        field_name = spec.get("field")
        if not isinstance(field_name, str) or not _FIELD_RE.match(field_name):
            raise LibraryError(f"action {action!r} verify.field is invalid")
        delta = spec.get("min_abs_delta", 1e-6)
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            raise LibraryError(f"action {action!r} verify.min_abs_delta must be numeric")
        if not math.isfinite(float(delta)) or delta < 0:
            raise LibraryError(
                f"action {action!r} verify.min_abs_delta must be finite and >= 0"
            )
        direction = spec.get("direction", "any")
        if direction not in _ALLOWED_DIRECTIONS:
            raise LibraryError(
                f"action {action!r} verify.direction must be one of {_ALLOWED_DIRECTIONS}"
            )
        metric = spec.get("metric", "delta")
        if metric not in ("delta", "distance_2d"):
            raise LibraryError(f"action {action!r} verify.metric must be delta or distance_2d")
        if metric == "distance_2d" and direction != "any":
            raise LibraryError("distance_2d requires direction=any")
        return VerifySpec(
            observation=observation,
            field=field_name,
            min_abs_delta=float(delta),
            direction=direction,
            metric=metric,
        )

    # ------------------------------------------------------------- accessors
    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.actions))

    @property
    def services(self) -> tuple[str, ...]:
        return tuple(sorted({a.service for a in self.actions.values()}))

    @property
    def has_verification(self) -> bool:
        return any(a.verify is not None for a in self.actions.values())

    @property
    def observation_names(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {a.verify.observation for a in self.actions.values() if a.verify is not None}
            )
        )

    def get(self, name: str) -> ActionSpec | None:
        return self.actions.get(name)

    def service_for(self, name: str) -> str:
        return self.actions[name].service

    def prompt_text(self) -> str:
        chunks = []
        for index, name in enumerate(self.names, start=1):
            action = self.actions[name]
            schema = {
                p.name: p.schema() for p in action.parameters
            }
            chunks.append(
                "\n".join(
                    [
                        f"# Atomic action {index}:",
                        f"Name: {name!r}",
                        f"ROS service: {action.service!r}",
                        "Description:",
                        action.description,
                        "Input (JSON object): " + dumps(schema),
                    ]
                )
            )
        return "\n\n".join(chunks)
