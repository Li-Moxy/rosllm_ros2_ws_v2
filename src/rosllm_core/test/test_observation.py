import json
import pytest
from types import SimpleNamespace

from rosllm_core.library import VerifySpec
from rosllm_core.observation import (
    META_PREFIX,
    ObservationView,
    entry_from_snapshot,
    verify_effect,
)


def message(name, payload=None, meta=None, raw_text=None):
    texts = []
    if raw_text is not None:
        texts.append(raw_text)
    elif payload is not None:
        texts.append(json.dumps(payload))
    if meta is not None:
        texts.append(META_PREFIX + json.dumps(meta))
    return SimpleNamespace(name=name, text=texts, image=[])


def view(*messages):
    return ObservationView.from_messages(messages)


def test_fresh_observation():
    v = view(
        message(
            "pose",
            {"x": 1.0},
            {"status": "fresh", "age_sec": 0.1, "seq": 3},
        )
    )
    entry = v.get("pose")
    assert entry.status == "fresh"
    assert entry.payload == {"x": 1.0}
    assert entry.age_sec == 0.1
    assert entry.seq == 3
    assert v.is_fresh("pose", 1.0)[0]


def test_stale_observation():
    v = view(
        message("pose", {"x": 1.0}, {"status": "stale", "age_sec": 9.0})
    )
    ok, reason = v.is_fresh("pose", 1.0)
    assert not ok and "stale" in reason


def test_missing_observation():
    v = view()
    assert v.status("pose") == "missing"
    ok, reason = v.is_fresh("pose")
    assert not ok and "missing" in reason


def test_unknown_age_without_metadata():
    v = view(message("pose", {"x": 1.0}))
    assert v.get("pose").status == "unknown_age"
    assert v.is_fresh("pose", 1.0)[0]
    assert not v.is_fresh("pose", 1.0, require_age=True)[0]


def test_invalid_metadata():
    v = view(message("pose", {"x": 1.0}, raw_text=META_PREFIX + "{bad"))
    # raw_text replaces the payload, and invalid metadata is reported as error.
    assert v.get("pose").status == "error"


def test_payload_must_be_json():
    v = view(message("pose", raw_text="not json", meta={"status": "fresh"}))
    assert v.get("pose").payload is None
    assert v.get("pose").status == "fresh"


def test_summary_includes_status_and_payload():
    v = view(
        message("a", {"x": 1}, {"status": "fresh", "age_sec": 0.2}),
        message("b", meta={"status": "missing"}),
    )
    summary = v.summary()
    assert "a [fresh, age 0.20s]" in summary
    assert "b [missing" in summary


def test_verify_effect_success():
    before = view(message("pose", {"x": 1.0}, {"status": "fresh", "age_sec": 0.0})).get("pose")
    after = view(message("pose", {"x": 1.5}, {"status": "fresh", "age_sec": 0.0})).get("pose")
    check = verify_effect(
        "forward",
        VerifySpec("pose", "x", 0.1, "any"),
        before,
        after,
        max_age_sec=1.0,
    )
    assert check.ok and check.delta == 0.5


def test_verify_effect_threshold_direction_and_errors():
    def entry(value, status="fresh", age=0.0):
        return view(
            message("pose", {"x": value}, {"status": status, "age_sec": age})
        ).get("pose")

    spec = VerifySpec("pose", "x", 0.1, "increase")
    assert not verify_effect("a", spec, entry(1.0), entry(1.05)).ok
    assert verify_effect("a", spec, entry(1.0), entry(1.2)).ok
    assert not verify_effect("a", spec, entry(1.2), entry(1.0)).ok
    assert not verify_effect("a", spec, entry(1.0), entry(1.5, status="stale")).ok
    assert not verify_effect("a", spec, None, entry(1.5)).ok
    assert not verify_effect("a", spec, entry(1.0), None).ok


def test_verify_effect_missing_field_and_non_numeric():
    spec = VerifySpec("pose", "y", 0.0, "any")
    before = view(message("pose", {"x": 1.0}, {"status": "fresh"})).get("pose")
    after = view(message("pose", {"x": 2.0}, {"status": "fresh"})).get("pose")
    assert not verify_effect("a", spec, before, after).ok
    before = view(message("pose", {"y": True}, {"status": "fresh"})).get("pose")
    after = view(message("pose", {"y": False}, {"status": "fresh"})).get("pose")
    check = verify_effect("a", spec, before, after)
    assert not check.ok and "not numeric" in check.reason


def test_entry_from_snapshot_round_trip():
    original = view(
        message("pose", {"x": 1.0}, {"status": "fresh", "age_sec": 0.3, "seq": 7})
    ).get("pose")
    rebuilt = entry_from_snapshot(original.snapshot())
    assert rebuilt.name == "pose"
    assert rebuilt.payload == {"x": 1.0}
    assert rebuilt.status == "fresh"
    assert rebuilt.age_sec == 0.3
    assert rebuilt.seq == 7


def test_entry_from_snapshot_rejects_bad_input():
    assert entry_from_snapshot(None) is None
    assert entry_from_snapshot({}) is None
    assert entry_from_snapshot({"name": "pose", "age_sec": "x"}) is not None


@pytest.mark.parametrize("end", [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)])
def test_planar_displacement_is_independent_of_heading(end):
    def position(x, y):
        return view(message("pose", {"position": {"x": x, "y": y}},
                            {"status": "fresh", "age_sec": 0.0})).get("pose")
    spec = VerifySpec("pose", "position", 0.05, metric="distance_2d")
    check = verify_effect("forward", spec, position(0, 0), position(*end))
    assert check.ok and check.delta == pytest.approx(1.0)


@pytest.mark.parametrize("point", [{"x": 0, "y": 0}, {"x": 1}, {"x": True, "y": 1},
                                   {"x": "bad", "y": 1}, {"x": float('inf'), "y": 1}])
def test_planar_displacement_rejects_no_motion_or_bad_coordinates(point):
    before = entry_from_snapshot({"name": "pose", "status": "fresh",
                                  "payload": {"position": {"x": 0, "y": 0}}})
    after = entry_from_snapshot({"name": "pose", "status": "fresh", "payload": {"position": point}})
    assert not verify_effect("forward", VerifySpec("pose", "position", 0.05,
                            metric="distance_2d"), before, after).ok
