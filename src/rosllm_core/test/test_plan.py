import textwrap

import pytest
import yaml

from rosllm_core.info import Info
from rosllm_core.library import ActionLibrary
from rosllm_core.plan import PlanError, parse_plan, to_ndjson


@pytest.fixture()
def library(tmp_path):
    path = tmp_path / "lib.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "actions": {
                    "forward": {
                        "service": "/test/forward",
                        "description": "Move forward.",
                        "parameters": {
                            "vel": {"type": "number", "min": -2, "max": 2}
                        },
                    },
                    "turn": {
                        "service": "/test/turn",
                        "description": "Turn.",
                        "parameters": {
                            "vel": {"type": "number", "min": -2, "max": 2}
                        },
                    },
                    "stop": {"service": "/test/stop", "description": "Stop."},
                },
            }
        ),
        encoding="utf-8",
    )
    return ActionLibrary.load(path)


def test_valid_plan(library):
    plan = parse_plan(
        '{"name": "forward", "input": {"vel": 1}}\n'
        '{"name": "turn", "input": "{\\"vel\\": 0.5}"}\n'
        '{"name": "stop"}',
        library,
    )
    assert [step.name for step in plan.steps] == ["forward", "turn", "stop"]
    assert plan.steps[1].input_text == '{"vel":0.5}'
    assert plan.steps[2].input_obj == {}


def test_blank_lines_ignored(library):
    plan = parse_plan('\n{"name": "stop"}\n\n', library)
    assert len(plan.steps) == 1


def test_empty_plan(library):
    with pytest.raises(PlanError) as exc:
        parse_plan("  \n\n", library)
    assert exc.value.info == Info.NULL_BEHAVIOR


def test_too_many_steps(library):
    text = "\n".join(['{"name": "stop"}'] * 3)
    with pytest.raises(PlanError) as exc:
        parse_plan(text, library, max_steps=2)
    assert exc.value.info == Info.LIMIT_EXCEEDED


def test_oversized_plan(library):
    text = '{"name": "stop"}'
    with pytest.raises(PlanError) as exc:
        parse_plan(text, library, max_bytes=5)
    assert exc.value.info == Info.LIMIT_EXCEEDED


def test_oversized_step(library):
    with pytest.raises(PlanError, match="larger than"):
        parse_plan('{"name": "stop"}', library, max_input_bytes=5)


@pytest.mark.parametrize(
    "text, match",
    [
        ("{bad}", "invalid JSON"),
        ('["stop"]', "must be a JSON object"),
        ('{"name": "stop", "extra": 1}', "unknown key"),
        ('{"input": {}}', "name"),
        ('{"name": 3}', "name"),
        ('{"name": "evil"}', "not in the allowed"),
        ('{"name": "forward", "input": {"vel": 9}}', "max"),
        ('{"name": "forward", "input": {"vel": NaN}}', "non-finite"),
        ('{"name": "forward", "input": {"vel": 1, "vel": 2}}', "duplicate"),
        ('{"name": "stop"}\n{"name": "forward", "input": {"vel": true}}', "number"),
    ],
)
def test_invalid_plans(library, text, match):
    with pytest.raises(PlanError, match=match):
        parse_plan(text, library)


def test_non_string_behavior(library):
    with pytest.raises(PlanError, match="string"):
        parse_plan(None, library)


def test_ndjson_round_trip(library):
    plan = parse_plan(
        '{"name": "forward", "input": {"vel": 1}}\n{"name": "turn", "input": {"vel": -0.25}}',
        library,
    )
    text = to_ndjson(plan)
    assert text.splitlines() == [
        '{"input":{"vel":1},"name":"forward"}',
        '{"input":{"vel":-0.25},"name":"turn"}',
    ]
    assert len(parse_plan(text, library).steps) == 2
