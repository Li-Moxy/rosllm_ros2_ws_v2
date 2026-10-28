"""Sequence executor: NDJSON plan -> sequential AtomicAction service calls."""

from __future__ import annotations

import time

from rosllm_srvs.srv import AtomicAction

from rosllm_core.action_client import CallStatus
from rosllm_core.info import Info
from rosllm_core.plan import parse_plan
from rosllm_core.report import ExecStatus, ExecutionReport, StepRecord

from .executor_base import ExecutorBase, spin_node


class SequenceExecutor(ExecutorBase):
    def __init__(self):
        super().__init__("sequence_executor")

    def _parse(self, behavior: str):
        return parse_plan(
            behavior,
            self.library,
            max_steps=self.max_steps,
            max_bytes=self.max_plan_bytes,
            max_input_bytes=self.max_step_input_bytes,
        )

    def _services_for(self, plan) -> list[str]:
        return sorted({step.spec.service for step in plan.steps})

    def _total_steps(self, plan) -> int:
        return len(plan.steps)

    def _execute(self, plan, plan_id: str, deadline: float) -> ExecutionReport:
        steps: list[StepRecord] = []
        previous_output = ""
        failure_info: int | None = None
        failure_message = ""
        for step in plan.steps:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure_info = int(Info.TIMEOUT)
                failure_message = (
                    f"plan deadline expired before step {step.index + 1} ({step.name})"
                )
                self.get_logger().warning(f"{plan_id}: {failure_message}")
                break
            record = self._run_step(step, previous_output, plan_id, deadline)
            steps.append(record)
            self.get_logger().info(
                f"{plan_id}: step {step.index + 1}/{len(plan.steps)} {step.name} "
                f"-> {record.status} in {record.elapsed_sec:.2f}s "
                f"({record.request_id})"
            )
            if record.status != "ok":
                failure_info = record.info
                failure_message = (
                    f"step {step.index + 1} ({step.name}) {record.status}: "
                    f"{record.error or record.output}".strip()
                )
                break
            previous_output = record.output
            if step.spec.verify is not None:
                record.observation = self._snapshot(
                    plan_id, record, step.spec.verify.observation, deadline
                )

        completed = sum(1 for record in steps if record.status == "ok")
        if failure_info is None:
            report = ExecutionReport(
                status=ExecStatus.OK,
                info=int(Info.OK),
                message=f"all {len(steps)} step(s) executed successfully",
                plan_id=plan_id,
                robot_id=self.robot_id,
                total_steps=len(plan.steps),
                completed_steps=completed,
                steps=steps,
            )
        else:
            report = ExecutionReport(
                status=_status_from_info(failure_info),
                info=failure_info,
                message=failure_message,
                plan_id=plan_id,
                robot_id=self.robot_id,
                total_steps=len(plan.steps),
                completed_steps=completed,
                steps=steps,
            )
        return report

    def _run_step(self, step, previous_output: str, plan_id: str, deadline: float) -> StepRecord:
        remaining = deadline - time.monotonic()
        timeout = min(self.step_timeout, remaining)
        record = StepRecord(
            index=step.index,
            name=step.name,
            request_id=f"{plan_id}:s{step.index}",
        )
        if timeout <= 0:
            record.status = "timeout"
            record.info = int(Info.TIMEOUT)
            record.error = "plan deadline expired before the call could start"
            return record

        request = AtomicAction.Request(
            input=step.input_text, prev_output=previous_output
        )
        result = self.host.call(
            step.spec.service,
            request,
            timeout,
            request_id=record.request_id,
        )
        record.elapsed_sec = result.elapsed_sec
        if result.status is CallStatus.OK:
            response = result.response
            record.output = getattr(response, "output", "") or ""
            if bool(getattr(response, "success", False)):
                record.status = "ok"
                record.info = int(Info.OK)
            else:
                record.status = "failed"
                record.info = int(Info.ERROR_ATOMIC_ACTION)
                record.error = record.output or "atomic action returned success=false"
        elif result.status is CallStatus.TIMEOUT:
            record.status = "timeout"
            record.info = int(Info.TIMEOUT)
            record.error = result.error
        elif result.status is CallStatus.UNAVAILABLE:
            record.status = "unavailable"
            record.info = int(Info.ATOMIC_ACTION_UNAVAILABLE)
            record.error = result.error
        elif result.status is CallStatus.CANCELLED:
            record.status = "cancelled"
            record.info = int(Info.STOPPED)
            record.error = result.error
        else:
            # The request may have reached the server; never claim it did not.
            record.status = "unknown"
            record.info = int(Info.UNKNOWN_STATE)
            record.error = result.error
        return record


def _status_from_info(info: int) -> ExecStatus:
    if info == Info.TIMEOUT:
        return ExecStatus.TIMEOUT
    if info == Info.ATOMIC_ACTION_UNAVAILABLE:
        return ExecStatus.UNAVAILABLE
    if info == Info.UNKNOWN_STATE:
        return ExecStatus.UNKNOWN
    if info == Info.BUSY:
        return ExecStatus.BUSY
    return ExecStatus.FAILED


def main():
    spin_node(SequenceExecutor)


if __name__ == "__main__":
    main()
