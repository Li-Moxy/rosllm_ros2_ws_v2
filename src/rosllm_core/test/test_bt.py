import time

import pytest
import yaml

from rosllm_core.bt import (
    ActionOutcome,
    BTBudget,
    BTInterpreter,
    NodeStatus,
    TreeError,
    compile_tree,
)
from rosllm_core.info import Info
from rosllm_core.library import ActionLibrary


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
                        "description": "Forward.",
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


class FakeInvoke:
    def __init__(self, outcomes=None, delay=0.0):
        self.calls = []
        self.outcomes = outcomes or {}
        self.delay = delay

    def __call__(self, spec, input_text, previous_output, timeout):
        self.calls.append((spec.name, input_text, previous_output, timeout))
        if self.delay:
            time.sleep(self.delay)
        result = self.outcomes.get(spec.name, "success")
        return ActionOutcome(
            result,
            output=f"{spec.name}-out",
            error="" if result == "success" else f"{spec.name}-{result}",
            elapsed_sec=self.delay,
        )


def run(xml, library, invoker, budget=None, deadline=5.0):
    tree = compile_tree(xml, library, budget)
    return tree, BTInterpreter(
        tree, invoker, budget=budget, default_action_timeout_sec=1.0
    ).run(time.monotonic() + deadline)


def test_sequence_and_previous_output(library):
    invoker = FakeInvoke()
    tree, result = run(
        """<Sequence>
             <Action name="forward" input='{"vel": 1}'/>
             <Action name="turn" input='{"vel": 1}'/>
           </Sequence>""",
        library,
        invoker,
    )
    assert result.status is NodeStatus.SUCCESS
    assert [call[0] for call in invoker.calls] == ["forward", "turn"]
    assert invoker.calls[1][2] == "forward-out"
    assert tree.worst_case_actions == 2
    assert result.action_calls == 2


def test_fallback(library):
    invoker = FakeInvoke({"forward": "failed"})
    _, result = run(
        """<Fallback>
             <Action name="forward" input='{"vel": 1}'/>
             <Action name="turn" input='{"vel": 1}'/>
           </Fallback>""",
        library,
        invoker,
    )
    assert result.status is NodeStatus.SUCCESS
    assert [call[0] for call in invoker.calls] == ["forward", "turn"]


def test_fallback_does_not_run_after_timeout(library):
    invoker = FakeInvoke({"forward": "timeout"})
    _, result = run(
        """<Fallback>
             <Action name="forward" input='{"vel": 1}'/>
             <Action name="turn" input='{"vel": 1}'/>
           </Fallback>""",
        library,
        invoker,
    )
    assert result.status is NodeStatus.ABORT
    assert result.info == Info.TIMEOUT
    assert len(invoker.calls) == 1


def test_retry_on_explicit_failure(library):
    invoker = FakeInvoke({"forward": "failed"})
    _, result = run(
        """<RetryUntilSuccessful num_attempts="3">
             <Action name="forward" input='{"vel": 1}'/>
           </RetryUntilSuccessful>""",
        library,
        invoker,
    )
    assert result.status is NodeStatus.FAILURE
    assert len(invoker.calls) == 3
    assert result.info == Info.ERROR_ATOMIC_ACTION


def test_retry_stops_on_timeout(library):
    invoker = FakeInvoke({"forward": "timeout"})
    _, result = run(
        """<RetryUntilSuccessful num_attempts="5">
             <Action name="forward" input='{"vel": 1}'/>
           </RetryUntilSuccessful>""",
        library,
        invoker,
    )
    assert result.info == Info.TIMEOUT
    assert len(invoker.calls) == 1


def test_timeout_node(library):
    invoker = FakeInvoke(delay=0.2)
    _, result = run(
        """<Timeout msec="100">
             <Action name="forward" input='{"vel": 1}'/>
           </Timeout>""",
        library,
        invoker,
        deadline=5.0,
    )
    assert result.status is NodeStatus.ABORT
    assert result.info == Info.TIMEOUT
    assert len(invoker.calls) == 1


def test_retry_budget_is_multiplied_before_execution(library):
    xml = """<RetryUntilSuccessful num_attempts="10">
               <Sequence>
                 <Action name="forward" input='{"vel": 1}'/>
                 <Action name="turn" input='{"vel": 1}'/>
                 <Action name="stop"/>
                 <Action name="forward" input='{"vel": 1}'/>
               </Sequence>
             </RetryUntilSuccessful>"""
    invoker = FakeInvoke()
    with pytest.raises(TreeError) as exc:
        compile_tree(xml, library, BTBudget(max_action_calls=32))
    assert exc.value.info == Info.LIMIT_EXCEEDED
    assert invoker.calls == []


def test_nested_retry_multiplication(library):
    tree = compile_tree(
        """<RetryUntilSuccessful num_attempts="2">
             <RetryUntilSuccessful num_attempts="2">
               <Action name="forward" input='{"vel": 1}'/>
             </RetryUntilSuccessful>
           </RetryUntilSuccessful>""",
        library,
        BTBudget(max_action_calls=4),
    )
    assert tree.worst_case_actions == 4
    with pytest.raises(TreeError):
        compile_tree(
            """<RetryUntilSuccessful num_attempts="2">
                 <RetryUntilSuccessful num_attempts="2">
                   <Action name="forward" input='{"vel": 1}'/>
                 </RetryUntilSuccessful>
               </RetryUntilSuccessful>""",
            library,
            BTBudget(max_action_calls=3),
        )


def test_invalid_later_action_rejected_before_any_call(library):
    invoker = FakeInvoke()
    xml = """<Sequence>
               <Action name="forward" input='{"vel": 1}'/>
               <Action name="evil"/>
             </Sequence>"""
    with pytest.raises(TreeError):
        run(xml, library, invoker)
    assert invoker.calls == []


def test_bad_input_rejected_before_any_call(library):
    invoker = FakeInvoke()
    with pytest.raises(TreeError):
        run(
            """<Sequence>
                 <Action name="forward" input='{"vel": 1}'/>
                 <Action name="turn" input='{"vel": 99}'/>
               </Sequence>""",
            library,
            invoker,
        )
    assert invoker.calls == []


@pytest.mark.parametrize(
    "xml, match",
    [
        ("<Sequence><Action name='forward'/></Sequence>", "input"),
        ("<Unknown><Action name='forward'/></Unknown>", "unsupported"),
        ("<Sequence bad='1'><Action name='stop'/></Sequence>", "attributes"),
        ("<Action name='forward' extra='1'/>", "attributes"),
        ("<Sequence/>", "at least one"),
        ("<RetryUntilSuccessful><Action name='stop'/></RetryUntilSuccessful>", "num_attempts"),
        ("<RetryUntilSuccessful num_attempts='0'><Action name='stop'/></RetryUntilSuccessful>", "1..10"),
        ("<Timeout><Action name='stop'/></Timeout>", "msec"),
        ("<Timeout msec='0'><Action name='stop'/></Timeout>", "1..600000"),
        ("<Sequence><Action name='stop'><Action name='stop'/></Action></Sequence>", "child"),
    ],
)
def test_invalid_trees(library, xml, match):
    with pytest.raises(TreeError, match=match):
        compile_tree(xml, library)


def test_malformed_xml_and_doctype(library):
    with pytest.raises(TreeError, match="invalid XML"):
        compile_tree("<Sequence>", library)
    with pytest.raises(TreeError, match="DOCTYPE"):
        compile_tree("<!DOCTYPE x><Sequence/>", library)


def test_node_and_depth_limits(library):
    with pytest.raises(TreeError) as exc:
        compile_tree(
            "<Sequence>" + "<Action name='stop'/>" * 5 + "</Sequence>",
            library,
            BTBudget(max_nodes=3),
        )
    assert exc.value.info == Info.LIMIT_EXCEEDED
    with pytest.raises(TreeError):
        compile_tree(
            "<Sequence><Sequence><Action name='stop'/></Sequence></Sequence>",
            library,
            BTBudget(max_depth=1),
        )


def test_oversized_xml(library):
    with pytest.raises(TreeError) as exc:
        compile_tree("<Sequence><Action name='stop'/></Sequence>", library, BTBudget(max_input_bytes=10))
    assert exc.value.info == Info.LIMIT_EXCEEDED


def test_multiple_trees_and_selection(library):
    xml = """<root main_tree_to_execute="B">
               <BehaviorTree ID="A"><Action name="stop"/></BehaviorTree>
               <BehaviorTree ID="B"><Action name="turn" input='{"vel": 0.1}'/></BehaviorTree>
             </root>"""
    tree = compile_tree(xml, library)
    invoker = FakeInvoke()
    result = BTInterpreter(tree, invoker).run(time.monotonic() + 5.0)
    assert result.status is NodeStatus.SUCCESS
    assert [call[0] for call in invoker.calls] == ["turn"]


def test_tree_wrapper_requires_one_child(library):
    with pytest.raises(TreeError, match="exactly one"):
        compile_tree(
            "<root><BehaviorTree><Action name='stop'/><Action name='stop'/></BehaviorTree></root>",
            library,
        )


def test_runtime_budget(library):
    invoker = FakeInvoke(delay=0.3)
    tree = compile_tree("<Action name='forward' input='{\"vel\": 1}'/>", library)
    result = BTInterpreter(
        tree, invoker, budget=BTBudget(max_runtime_sec=0.1)
    ).run(time.monotonic() + 5.0)
    assert result.status is NodeStatus.ABORT
    assert result.info == Info.TIMEOUT
    assert len(invoker.calls) == 1


def test_on_step_callback(library):
    seen = []
    tree = compile_tree("<Action name='stop'/>", library)
    result = BTInterpreter(
        tree, FakeInvoke(), on_step=seen.append
    ).run(time.monotonic() + 5.0)
    assert result.status is NodeStatus.SUCCESS
    assert len(seen) == 1 and seen[0].name == "stop"


def test_unknown_outcome_is_abort(library):
    invoker = FakeInvoke({"forward": "unknown"})
    _, result = run("<Action name='forward' input='{\"vel\": 1}'/>", library, invoker)
    assert result.status is NodeStatus.ABORT
    assert result.info == Info.UNKNOWN_STATE
