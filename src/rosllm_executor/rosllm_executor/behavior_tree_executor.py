"""Behaviour-tree executor using the safe, limited interpreter in rosllm_core.

Before any action is invoked, the whole XML tree is compiled and validated,
including whitelist membership, input schemas, node/depth limits and the
worst-case number of action calls after nested retries.  Timeout/unknown
outcomes abort the tree instead of triggering fallback/retry.
"""

from __future__ import annotations

import math
import time

from rosllm_srvs.srv import AtomicAction

from rosllm_core.action_client import CallStatus
from rosllm_core.bt import (
    ActionOutcome,
    BTBudget,
    BTInterpreter,
    NodeStatus,
    compile_tree,
)
from rosllm_core.info import Info
from rosllm_core.report import ExecStatus, ExecutionReport, status_for_info

from .executor_base import ExecutorBase, spin_node


class BehaviorTreeExecutor(ExecutorBase):
    def __init__(self):
        super().__init__("behavior_tree_executor")
        self.declare_parameter("bt_max_nodes", 64)
        self.declare_parameter("bt_max_depth", 12)
        self.declare_parameter("bt_max_action_calls", 32)
        self.declare_parameter("bt_max_runtime_sec", 60.0)
        self.declare_parameter("bt_max_input_bytes", 32768)
        self.bt_budget = BTBudget(
            max_nodes=int(self.get_parameter("bt_max_nodes").value),
            max_depth=int(self.get_parameter("bt_max_depth").value),
            max_action_calls=int(self.get_parameter("bt_max_action_calls").value),
            max_runtime_sec=float(self.get_parameter("bt_max_runtime_sec").value),
            max_input_bytes=int(self.get_parameter("bt_max_input_bytes").value),
        )
        self._validate_bt_budget()

    def _validate_bt_budget(self) -> None:
        budget = self.bt_budget
        if not 1 <= budget.max_nodes <= 512:
            raise RuntimeError("bt_max_nodes must be within 1..512")
        if not 1 <= budget.max_depth <= 64:
            raise RuntimeError("bt_max_depth must be within 1..64")
        if not 1 <= budget.max_action_calls <= 256:
            raise RuntimeError("bt_max_action_calls must be within 1..256")
        if not math.isfinite(budget.max_runtime_sec) or not 0.1 <= budget.max_runtime_sec <= 3600:
            raise RuntimeError("bt_max_runtime_sec must be within 0.1..3600")
        if not 1024 <= budget.max_input_bytes <= 256 * 1024:
            raise RuntimeError("bt_max_input_bytes must be within 1024..262144")

    def _parse(self, behavior: str):
        return compile_tree(behavior, self.library, self.bt_budget)

    def _services_for(self, tree) -> list[str]:
        return sorted({spec.service for spec in tree.actions})

    def _total_steps(self, tree) -> int:
        return tree.worst_case_actions

    def _execute(self, tree, plan_id: str, deadline: float) -> ExecutionReport:
        calls = {"count": 0}

        def invoke(spec, input_text: str, previous_output: str, timeout: float) -> ActionOutcome:
            calls["count"] += 1
            request_id = f"{plan_id}:bt{calls['count']}"
            result = self.host.call(
                spec.service,
                AtomicAction.Request(input=input_text, prev_output=previous_output),
                timeout,
                request_id=request_id,
            )
            if result.status is CallStatus.OK:
                response = result.response
                output = getattr(response, "output", "") or ""
                if bool(getattr(response, "success", False)):
                    return ActionOutcome(
                        "success", output=output, request_id=request_id,
                        elapsed_sec=result.elapsed_sec, info=int(Info.OK),
                    )
                return ActionOutcome(
                    "failed",
                    output=output,
                    error=output or "atomic action returned success=false",
                    request_id=request_id,
                    elapsed_sec=result.elapsed_sec,
                    info=int(Info.ERROR_ATOMIC_ACTION),
                )
            if result.status is CallStatus.TIMEOUT:
                return ActionOutcome(
                    "timeout", error=result.error, request_id=request_id,
                    elapsed_sec=result.elapsed_sec, info=int(Info.TIMEOUT),
                )
            if result.status is CallStatus.UNAVAILABLE:
                return ActionOutcome(
                    "unavailable", error=result.error, request_id=request_id,
                    elapsed_sec=result.elapsed_sec,
                    info=int(Info.ATOMIC_ACTION_UNAVAILABLE),
                )
            if result.status is CallStatus.CANCELLED:
                return ActionOutcome(
                    "cancelled", error=result.error, request_id=request_id,
                    elapsed_sec=result.elapsed_sec, info=int(Info.STOPPED),
                )
            return ActionOutcome(
                "unknown", error=result.error, request_id=request_id,
                elapsed_sec=result.elapsed_sec, info=int(Info.UNKNOWN_STATE),
            )

        def on_step(step) -> None:
            spec = self.library.get(step.name)
            if spec is None or spec.verify is None or step.status != "success":
                return
            step.observation = self._snapshot(
                plan_id, step, spec.verify.observation, deadline
            )
            self.get_logger().info(
                f"{plan_id}: bt action {step.name} ({step.request_id}) -> "
                f"{step.status} in {step.elapsed_sec:.2f}s"
            )

        interpreter = BTInterpreter(
            tree,
            invoke,
            budget=self.bt_budget,
            default_action_timeout_sec=self.step_timeout,
            on_step=on_step,
        )
        result = interpreter.run(deadline)
        completed = sum(1 for step in result.steps if step.status == "success")
        if result.status is NodeStatus.SUCCESS:
            status = ExecStatus.OK
            info = int(Info.OK)
            message = (
                f"behaviour tree succeeded ({result.action_calls}/"
                f"{tree.worst_case_actions} worst-case action calls)"
            )
        elif result.status is NodeStatus.FAILURE:
            status = ExecStatus.FAILED
            info = int(Info.ERROR_ATOMIC_ACTION)
            message = result.message or "behaviour tree failed"
        else:
            status = status_for_info(result.info)
            info = result.info
            message = result.message or "behaviour tree aborted"
        return ExecutionReport(
            status=status,
            info=info,
            message=message,
            plan_id=plan_id,
            robot_id=self.robot_id,
            total_steps=tree.worst_case_actions,
            completed_steps=completed,
            elapsed_sec=result.elapsed_sec,
            steps=result.steps,
        )


def main():
    spin_node(BehaviorTreeExecutor)


if __name__ == "__main__":
    main()
