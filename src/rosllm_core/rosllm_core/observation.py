"""Observation handling: freshness, missing data and post-condition checks.

The ROSLLM message and service definitions are kept byte-for-byte compatible
with upstream, so freshness metadata is carried as a reserved text element
inside each ``rosllm_msgs/Observation``:

    __rosllm_meta__:{"status":"fresh","age_sec":0.08,"seq":42}

Producers that do not add metadata are still accepted and reported as
``unknown_age``; consumers can require fresh, age-stamped observations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

from .jsonutil import StrictJSONError, dumps, loads
from .library import VerifySpec

META_PREFIX = "__rosllm_meta__:"

FRESH = "fresh"
STALE = "stale"
MISSING = "missing"
ERROR = "error"
UNKNOWN_AGE = "unknown_age"


@dataclass(frozen=True)
class ObservationEntry:
    name: str
    texts: tuple[str, ...] = ()
    payload: Any | None = None
    status: str = UNKNOWN_AGE
    age_sec: float | None = None
    detail: str = ""
    seq: int | None = None
    has_meta: bool = False

    def is_fresh(
        self, max_age_sec: float | None = None, require_age: bool = False
    ) -> tuple[bool, str]:
        if self.status in (MISSING, STALE, ERROR):
            return False, f"observation {self.name!r} is {self.status}: {self.detail}".strip()
        if self.status == UNKNOWN_AGE:
            if require_age:
                return False, f"observation {self.name!r} has no freshness metadata"
            return True, "freshness unknown (no metadata)"
        if self.age_sec is None:
            if require_age:
                return False, f"observation {self.name!r} has no age"
            return True, "age unknown"
        if max_age_sec is not None and self.age_sec > max_age_sec:
            return False, (
                f"observation {self.name!r} is stale: age {self.age_sec:.2f}s > "
                f"{max_age_sec:.2f}s"
            )
        return True, ""

    def snapshot(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "age_sec": self.age_sec,
            "payload": self.payload,
            "detail": self.detail,
            "seq": self.seq,
        }


def entry_from_snapshot(data: Any) -> ObservationEntry | None:
    if not isinstance(data, dict):
        return None
    name = data.get("name", "")
    if not isinstance(name, str) or not name:
        return None
    status = data.get("status", UNKNOWN_AGE)
    if status not in (FRESH, STALE, MISSING, ERROR, UNKNOWN_AGE):
        status = ERROR
    age = data.get("age_sec")
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, (int, float)):
            age = None
        elif not math.isfinite(float(age)) or age < 0:
            age = None
    payload = data.get("payload")
    detail = data.get("detail") if isinstance(data.get("detail"), str) else ""
    seq = data.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        seq = None
    return ObservationEntry(
        name=name,
        texts=(),
        payload=payload,
        status=status,
        age_sec=None if age is None else float(age),
        detail=detail,
        seq=seq,
        has_meta=status != UNKNOWN_AGE,
    )


class ObservationView:
    def __init__(self, entries: Iterable[ObservationEntry] = ()):
        self.entries: dict[str, ObservationEntry] = {}
        for entry in entries:
            self.entries[entry.name] = entry

    @classmethod
    def from_messages(cls, messages: Iterable[Any]) -> "ObservationView":
        """Build a view from ``rosllm_msgs/Observation`` messages."""
        entries = []
        for message in messages or ():
            name = getattr(message, "name", "") or ""
            if not isinstance(name, str) or not name:
                continue
            texts = tuple(
                text for text in (getattr(message, "text", None) or ()) if isinstance(text, str)
            )
            entries.append(_build_entry(name, texts))
        return cls(entries)

    def get(self, name: str) -> ObservationEntry | None:
        return self.entries.get(name)

    def status(self, name: str) -> str:
        entry = self.entries.get(name)
        return entry.status if entry else MISSING

    def payload(self, name: str) -> Any | None:
        entry = self.entries.get(name)
        return entry.payload if entry else None

    def is_fresh(
        self,
        name: str,
        max_age_sec: float | None = None,
        require_age: bool = False,
    ) -> tuple[bool, str]:
        entry = self.entries.get(name)
        if entry is None:
            return False, f"observation {name!r} is missing"
        return entry.is_fresh(max_age_sec, require_age)

    def summary(self, max_payload_bytes: int = 4096, max_entries: int = 20) -> str:
        if not self.entries:
            return "(no observations received)"
        lines = []
        for name in sorted(self.entries)[:max_entries]:
            entry = self.entries[name]
            if entry.payload is None:
                body = entry.detail or "(no payload)"
            else:
                try:
                    body = dumps(entry.payload)
                except (TypeError, ValueError):
                    body = str(entry.payload)
                if len(body.encode("utf-8")) > max_payload_bytes:
                    body = body[:max_payload_bytes] + "...(truncated)"
            age = "age unknown" if entry.age_sec is None else f"age {entry.age_sec:.2f}s"
            lines.append(f"- {name} [{entry.status}, {age}]: {body}")
        return "\n".join(lines)


def _build_entry(name: str, texts: tuple[str, ...]) -> ObservationEntry:
    meta = None
    meta_error = ""
    payload_text = None
    for text in texts:
        if text.startswith(META_PREFIX):
            if meta is not None:
                meta_error = "multiple metadata entries; using the first"
                continue
            try:
                candidate = loads(text[len(META_PREFIX) :])
            except StrictJSONError as exc:
                meta_error = f"invalid observation metadata: {exc}"
                continue
            if not isinstance(candidate, dict):
                meta_error = "observation metadata is not an object"
                continue
            meta = candidate
        elif payload_text is None:
            payload_text = text

    payload = None
    if payload_text is not None:
        try:
            payload = loads(payload_text)
        except StrictJSONError:
            payload = None

    if meta is None:
        return ObservationEntry(
            name=name,
            texts=texts,
            payload=payload,
            status=ERROR if meta_error else UNKNOWN_AGE,
            detail=meta_error or ("" if payload_text is not None else "no payload"),
        )

    status = meta.get("status", FRESH)
    if status == "ok":
        status = FRESH
    if status not in (FRESH, STALE, MISSING, ERROR):
        status = ERROR
    age = meta.get("age_sec")
    if age is not None:
        if isinstance(age, bool) or not isinstance(age, (int, float)):
            age = None
        elif not math.isfinite(float(age)) or age < 0:
            age = None
    seq = meta.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        seq = None
    detail = meta.get("detail") if isinstance(meta.get("detail"), str) else meta_error
    return ObservationEntry(
        name=name,
        texts=texts,
        payload=payload,
        status=status,
        age_sec=None if age is None else float(age),
        detail=detail,
        seq=seq,
        has_meta=True,
    )


@dataclass(frozen=True)
class VerificationCheck:
    action: str
    observation: str
    field: str
    ok: bool
    reason: str
    before: Any = None
    after: Any = None
    delta: float | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "observation": self.observation,
            "field": self.field,
            "ok": self.ok,
            "reason": self.reason,
            "delta": self.delta,
        }


def _lookup(payload: Any, field: str) -> tuple[bool, Any]:
    current = payload
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def verify_effect(
    action: str,
    verify: VerifySpec,
    before: ObservationEntry | None,
    after: ObservationEntry | None,
    *,
    max_age_sec: float | None = None,
    require_age: bool = False,
) -> VerificationCheck:
    if before is None:
        return VerificationCheck(
            action, verify.observation, verify.field, False, "before observation is missing"
        )
    if after is None:
        return VerificationCheck(
            action, verify.observation, verify.field, False, "after observation is missing"
        )
    for label, entry in (("before", before), ("after", after)):
        fresh, reason = entry.is_fresh(max_age_sec, require_age)
        if not fresh:
            return VerificationCheck(
                action,
                verify.observation,
                verify.field,
                False,
                f"{label} {reason}",
            )
        if not isinstance(entry.payload, dict):
            return VerificationCheck(
                action,
                verify.observation,
                verify.field,
                False,
                f"{label} observation payload is not a JSON object",
            )
    ok_before, value_before = _lookup(before.payload, verify.field)
    ok_after, value_after = _lookup(after.payload, verify.field)
    if not ok_before or not ok_after:
        return VerificationCheck(
            action,
            verify.observation,
            verify.field,
            False,
            f"field {verify.field!r} is missing from the observation payload",
        )
    if verify.metric == "distance_2d":
        if not all(isinstance(point, dict) and "x" in point and "y" in point
                   for point in (value_before, value_after)):
            return VerificationCheck(
                action, verify.observation, verify.field, False,
                "distance_2d requires objects containing numeric x and y",
            )
        values = [point[axis] for point in (value_before, value_after) for axis in ("x", "y")]
    else:
        values = (value_before, value_after)
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return VerificationCheck(
                action,
                verify.observation,
                verify.field,
                False,
                f"field {verify.field!r} is not numeric",
                value_before,
                value_after,
            )
        if not math.isfinite(float(value)):
            return VerificationCheck(
                action,
                verify.observation,
                verify.field,
                False,
                f"field {verify.field!r} is not finite",
                value_before,
                value_after,
            )
    if verify.metric == "distance_2d":
        delta = math.hypot(
            float(value_after["x"]) - float(value_before["x"]),
            float(value_after["y"]) - float(value_before["y"]),
        )
    else:
        delta = float(value_after) - float(value_before)
    if abs(delta) <= verify.min_abs_delta:
        return VerificationCheck(
            action,
            verify.observation,
            verify.field,
            False,
            f"observed change {delta:+.4f} is not greater than {verify.min_abs_delta}",
            value_before,
            value_after,
            delta,
        )
    if verify.direction == "increase" and delta <= 0:
        return VerificationCheck(
            action,
            verify.observation,
            verify.field,
            False,
            f"expected an increase, observed {delta:+.4f}",
            value_before,
            value_after,
            delta,
        )
    if verify.direction == "decrease" and delta >= 0:
        return VerificationCheck(
            action,
            verify.observation,
            verify.field,
            False,
            f"expected a decrease, observed {delta:+.4f}",
            value_before,
            value_after,
            delta,
        )
    return VerificationCheck(
        action,
        verify.observation,
        verify.field,
        True,
        f"observed change {delta:+.4f}",
        value_before,
        value_after,
        delta,
    )
