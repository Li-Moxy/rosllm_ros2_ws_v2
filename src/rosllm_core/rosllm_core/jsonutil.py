"""Strict JSON helpers.

``json.loads`` accepts ``NaN``/``Infinity`` and silently keeps the last value
for duplicate keys.  Both are dangerous for robot command plans, so the whole
port uses these helpers instead.
"""

import json


class StrictJSONError(ValueError):
    """Raised when text is not strict, finite, duplicate-free JSON."""


def _reject_constant(name):
    raise StrictJSONError(f"non-finite JSON number {name!r} is not allowed")


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def loads(text):
    """Parse strict JSON from ``text``.

    Rejects duplicate keys, NaN/Infinity and over-deep nesting.
    """
    if not isinstance(text, str):
        raise StrictJSONError("JSON input must be a string")
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise StrictJSONError(
            f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}"
        ) from None
    except RecursionError:
        raise StrictJSONError("JSON nesting is too deep") from None


def dumps(obj):
    """Compact, deterministic, finite-only JSON encoding."""
    return json.dumps(
        obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False, sort_keys=True
    )
