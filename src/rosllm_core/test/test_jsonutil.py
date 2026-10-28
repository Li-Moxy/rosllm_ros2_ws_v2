import math

import pytest

from rosllm_core.jsonutil import StrictJSONError, dumps, loads


def test_valid_round_trip():
    value = {"a": 1, "b": [True, None, "x"]}
    assert loads(dumps(value)) == value


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", '{"a": NaN}'])
def test_rejects_non_finite(text):
    with pytest.raises(StrictJSONError):
        loads(text)


def test_rejects_duplicate_keys():
    with pytest.raises(StrictJSONError, match="duplicate"):
        loads('{"a": 1, "a": 2}')


def test_rejects_invalid_json():
    with pytest.raises(StrictJSONError, match="invalid JSON"):
        loads('{"a": }')


def test_rejects_non_string():
    with pytest.raises(StrictJSONError):
        loads(b"{}")


def test_rejects_deep_nesting():
    with pytest.raises(StrictJSONError, match="too deep"):
        loads("[" * 4000 + "]" * 4000)


def test_dumps_rejects_nan():
    with pytest.raises(ValueError):
        dumps({"x": math.nan})
