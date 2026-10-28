import textwrap

import pytest
import yaml

from rosllm_core.library import ActionLibrary, LibraryError


def write(tmp_path, data, name="lib.yaml"):
    path = tmp_path / name
    if isinstance(data, str):
        path.write_text(textwrap.dedent(data), encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def base_data():
    return {
        "version": 1,
        "actions": {
            "forward": {
                "service": "/test/forward",
                "description": "Move forward.",
                "parameters": {
                    "vel": {
                        "type": "number",
                        "required": True,
                        "min": -2.0,
                        "max": 2.0,
                    }
                },
                "verify": {
                    "observation": "pose",
                    "field": "x",
                    "min_abs_delta": 0.01,
                    "direction": "any",
                },
            },
            "stop": {
                "service": "/test/stop",
                "description": "Stop.",
            },
        },
    }


def test_load_and_accessors(tmp_path):
    library = ActionLibrary.load(write(tmp_path, base_data()))
    assert library.names == ("forward", "stop")
    assert library.services == ("/test/forward", "/test/stop")
    assert library.has_verification
    assert library.observation_names == ("pose",)
    assert library.service_for("forward") == "/test/forward"
    text = library.prompt_text()
    assert "'forward'" in text
    assert "/test/forward" in text
    assert '"vel"' in text


def test_planar_verification_config(tmp_path):
    data = base_data()
    data["actions"]["forward"]["verify"].update(field="position", metric="distance_2d")
    spec = ActionLibrary.load(write(tmp_path, data)).get("forward").verify
    assert spec.metric == "distance_2d" and spec.field == "position"
    data["actions"]["forward"]["verify"]["direction"] = "increase"
    with pytest.raises(LibraryError, match="direction=any"):
        ActionLibrary.load(write(tmp_path, data))
    data["actions"]["forward"]["verify"].update(direction="any", metric="invalid")
    with pytest.raises(LibraryError, match="verify.metric"):
        ActionLibrary.load(write(tmp_path, data))


def test_description_file(tmp_path):
    (tmp_path / "forward.txt").write_text("File description.", encoding="utf-8")
    data = base_data()
    del data["actions"]["forward"]["description"]
    data["actions"]["forward"]["description_file"] = "forward.txt"
    library = ActionLibrary.load(write(tmp_path, data))
    assert library.get("forward").description == "File description."


def test_validate_input(tmp_path):
    library = ActionLibrary.load(write(tmp_path, base_data()))
    ok, value, error = library.get("forward").validate_input('{"vel": 1.5}')
    assert ok and value == {"vel": 1.5} and not error
    ok, value, _ = library.get("forward").validate_input({"vel": 1})
    assert ok and value == {"vel": 1}
    ok, _, error = library.get("forward").validate_input({"vel": 3})
    assert not ok and "max" in error
    ok, _, error = library.get("forward").validate_input({"vel": True})
    assert not ok and "number" in error
    ok, _, error = library.get("forward").validate_input({"other": 1})
    assert not ok and "unknown" in error
    ok, _, error = library.get("forward").validate_input("{}")
    assert not ok and "missing" in error
    ok, value, _ = library.get("stop").validate_input("")
    assert ok and value == {}


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda d: d.update(version=2), "version"),
        (lambda d: d.pop("actions"), "actions"),
        (lambda d: d.update(extra=1), "unknown top-level"),
        (lambda d: d["actions"].update({"bad name": {}}), "invalid action name"),
        (lambda d: d["actions"]["stop"].pop("service"), "service"),
        (lambda d: d["actions"]["stop"].update(service="bad service!"), "service"),
        (lambda d: d["actions"]["stop"].pop("description"), "exactly one"),
        (
            lambda d: d["actions"]["stop"].update(description="x", description_file="x.txt"),
            "exactly one",
        ),
        (lambda d: d["actions"]["stop"].update(unknown=1), "unknown key"),
        (
            lambda d: d["actions"]["forward"]["parameters"]["vel"].update(type="float"),
            "type must be",
        ),
        (
            lambda d: d["actions"]["forward"]["parameters"]["vel"].update(min=3, max=1),
            "min > max",
        ),
        (
            lambda d: d["actions"]["stop"].update(parameters={"s": {"type": "string", "min": 1}}),
            "min/max only",
        ),
        (
            lambda d: d["actions"]["stop"].update(parameters={"s": {"type": "string", "max_length": 0}}),
            "max_length",
        ),
        (
            lambda d: d["actions"]["forward"]["verify"].update(direction="sideways"),
            "direction",
        ),
        (
            lambda d: d["actions"]["forward"]["verify"].update(field="x; rm"),
            "verify.field",
        ),
        (
            lambda d: d["actions"]["forward"]["verify"].update(min_abs_delta=-1),
            "min_abs_delta",
        ),
    ],
)
def test_rejects_invalid_library(tmp_path, mutate, match):
    data = base_data()
    mutate(data)
    with pytest.raises(LibraryError, match=match):
        ActionLibrary.load(write(tmp_path, data))


def test_rejects_nan_parameter_bound(tmp_path):
    text = """
    version: 1
    actions:
      a:
        service: /a
        description: x
        parameters:
          v: {type: number, min: .nan}
    """
    with pytest.raises(LibraryError, match="finite"):
        ActionLibrary.load(write(tmp_path, text))


def test_rejects_missing_file(tmp_path):
    with pytest.raises(LibraryError, match="cannot read"):
        ActionLibrary.load(tmp_path / "nope.yaml")


def test_rejects_too_many_actions(tmp_path):
    data = {
        "version": 1,
        "actions": {
            f"a{i}": {"service": f"/a{i}", "description": "x"} for i in range(5)
        },
    }
    with pytest.raises(LibraryError, match="too many"):
        ActionLibrary.load(write(tmp_path, data), max_actions=4)
