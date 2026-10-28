"""Agent task state machine with explicit outcomes and post-condition checks."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from rosllm_srvs.srv import ExecuteBehavior, LLM, Observation

from rosllm_core.action_client import CallStatus, ClientHost
from rosllm_core.info import Info
from rosllm_core.library import ActionLibrary
from rosllm_core.observation import (
    ObservationView,
    VerificationCheck,
    entry_from_snapshot,
    verify_effect,
)
from rosllm_core.plan import Plan, PlanError, parse_plan, to_ndjson
from rosllm_core.report import ExecStatus, ExecutionReport, parse_report

from .prompt import build_prompt, extract_plan_text


class TaskStatus(str, Enum):
    SUCCESS_VERIFIED = "success_verified"
    SUCCESS_PARTIALLY_VERIFIED = "success_partially_verified"
    SUCCESS_UNVERIFIED = "success_unverified"
    POSTCONDITION_FAILED = "postcondition_failed"
    VERIFICATION_UNAVAILABLE = "verification_unavailable"
    PLAN_REJECTED = "plan_rejected"
    LLM_ERROR = "llm_error"
    LLM_TIMEOUT = "llm_timeout"
    OBSERVATION_UNAVAILABLE = "observation_unavailable"
    EXECUTION_FAILED = "execution_failed"
    EXECUTION_TIMEOUT = "execution_timeout"
    EXECUTION_UNKNOWN = "execution_unknown"
    EXECUTOR_UNAVAILABLE = "executor_unavailable"
    BUSY = "busy"
    INVALID_INPUT = "invalid_input"
    INTERNAL_ERROR = "internal_error"

    @property
    def is_success(self) -> bool:
        return self in (
            TaskStatus.SUCCESS_VERIFIED,
            TaskStatus.SUCCESS_PARTIALLY_VERIFIED,
            TaskStatus.SUCCESS_UNVERIFIED,
        )

    @property
    def exit_code(self) -> int:
        if self.is_success:
            return 0
        if self in (
            TaskStatus.EXECUTION_TIMEOUT,
            TaskStatus.EXECUTION_UNKNOWN,
            TaskStatus.LLM_TIMEOUT,
        ):
            return 3
        if self is TaskStatus.BUSY:
            return 4
        if self is TaskStatus.INTERNAL_ERROR:
            return 1
        return 2


@dataclass
class TaskReport:
    status: TaskStatus
    message: str
    info: int = int(Info.OK)
    plan_id: str = ""
    plan: list[dict] = field(default_factory=list)
    execution: dict | None = None
    verification: list[dict] = field(default_factory=list)
    unverified_actions: list[str] = field(default_factory=list)
    observations: str = ""
    llm_elapsed_sec: float = 0.0
    elapsed_sec: float = 0.0

    @property
    def exit_code(self) -> int:
        return self.status.exit_code

    def to_dict(self) -> dict:
        return {
            "schema": "rosllm.agent.v1",
            "plan_id": self.plan_id,
            "status": self.status.value,
            "info": self.info,
            "message": self.message,
            "plan": self.plan,
            "execution": self.execution,
            "verification": self.verification,
            "unverified_actions": self.unverified_actions,
            "observations": self.observations,
            "llm_elapsed_sec": round(self.llm_elapsed_sec, 6),
            "elapsed_sec": round(self.elapsed_sec, 6),
            "exit_code": self.exit_code,
        }


@dataclass(frozen=True)
class AgentConfig:
    library: ActionLibrary
    llm_service: str = "/call_llm"
    execute_service: str = "/execute_behavior"
    observation_service: str = "/get_observation"
    max_steps: int = 8
    max_plan_bytes: int = 16384
    llm_timeout_sec: float = 60.0
    execute_timeout_sec: float = 95.0
    observation_timeout_sec: float = 2.0
    max_observation_age_sec: float = 3.0
    require_observation_age: bool = False
    require_fresh_observation: bool = False
    use_observations: bool = True
    expert_advice: str = ""
    max_task_bytes: int = 4096


class Agent:
    def __init__(self, config: AgentConfig, host: ClientHost, logger):
        self.config = config
        self.host = host
        self.logger = logger
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._task_lock = threading.Lock()
        self._closed = False

    def run_task(self, task: str) -> TaskReport:
        started = time.monotonic()
        if not self._task_lock.acquire(blocking=False):
            return TaskReport(
                status=TaskStatus.BUSY,
                message="another task is already running in this agent",
                elapsed_sec=time.monotonic() - started,
            )
        try:
            report = self._run_task(task)
        except Exception as exc:  # noqa: BLE001 - CLI must not crash
            self.logger.error(
                f"agent internal error: {type(exc).__name__}: {exc}", exc_info=True
            )
            report = TaskReport(
                status=TaskStatus.INTERNAL_ERROR,
                message=f"internal agent error: {type(exc).__name__}: {exc}",
                info=int(Info.UNKNOWN_STATE),
            )
        finally:
            self._task_lock.release()
        report.elapsed_sec = time.monotonic() - started
        return report

    # ------------------------------------------------------------------ flow
    def _run_task(self, task: str) -> TaskReport:
        task = task.strip() if isinstance(task, str) else ""
        if not task:
            return TaskReport(TaskStatus.INVALID_INPUT, "task is empty")
        if len(task.encode("utf-8")) > self.config.max_task_bytes:
            return TaskReport(
                TaskStatus.INVALID_INPUT,
                f"task exceeds {self.config.max_task_bytes} bytes",
            )
        with self._counter_lock:
            self._counter += 1
            plan_id = f"agent-{os.getpid()}-{self._counter}"

        obs_view = ObservationView()
        obs_ok = False
        obs_error = ""
        if self.config.use_observations:
            result = self.host.call(
                self.config.observation_service,
                Observation.Request(),
                self.config.observation_timeout_sec,
                request_id=f"{plan_id}:obs-pre",
            )
            if result.ok:
                obs_view = ObservationView.from_messages(result.response.observation)
                obs_ok = True
            else:
                obs_error = f"{result.status.value}: {result.error}"
                self.logger.warning(f"{plan_id}: observation unavailable: {obs_error}")

        required = set(self.config.library.observation_names)
        if required:
            if not obs_ok:
                return TaskReport(
                    TaskStatus.OBSERVATION_UNAVAILABLE,
                    f"action library requires observations {sorted(required)}, "
                    f"but get_observation failed ({obs_error})",
                    info=int(Info.UNKNOWN_STATE),
                    plan_id=plan_id,
                    observations=obs_error,
                )
            for name in sorted(required):
                fresh, reason = obs_view.is_fresh(
                    name,
                    self.config.max_observation_age_sec,
                    self.config.require_observation_age,
                )
                if not fresh:
                    return TaskReport(
                        TaskStatus.OBSERVATION_UNAVAILABLE,
                        f"cannot plan safely: {reason}",
                        info=int(Info.UNKNOWN_STATE),
                        plan_id=plan_id,
                        observations=obs_view.summary(),
                    )
        elif self.config.require_fresh_observation and not obs_ok:
            return TaskReport(
                TaskStatus.OBSERVATION_UNAVAILABLE,
                f"get_observation failed ({obs_error})",
                info=int(Info.UNKNOWN_STATE),
                plan_id=plan_id,
                observations=obs_error,
            )

        observations_text = (
            obs_view.summary()
            if obs_ok
            else f"(observations unavailable: {obs_error})"
        )
        prompt = build_prompt(
            self.config.library.prompt_text(),
            observations_text,
            task,
            self.config.expert_advice,
        )
        llm_result = self.host.call(
            self.config.llm_service,
            LLM.Request(prompt=prompt),
            self.config.llm_timeout_sec,
            request_id=f"{plan_id}:llm",
        )
        llm_elapsed = llm_result.elapsed_sec
        if not llm_result.ok:
            status = (
                TaskStatus.LLM_TIMEOUT
                if llm_result.status is CallStatus.TIMEOUT
                else TaskStatus.LLM_ERROR
            )
            return TaskReport(
                status=status,
                message=f"LLM call failed: {llm_result.error}",
                info=int(Info.TIMEOUT)
                if llm_result.status is CallStatus.TIMEOUT
                else int(Info.UNKNOWN_STATE),
                plan_id=plan_id,
                observations=observations_text,
                llm_elapsed_sec=llm_elapsed,
            )
        llm_response = llm_result.response.response
        if not bool(getattr(llm_response, "success", False)):
            return TaskReport(
                TaskStatus.LLM_ERROR,
                f"LLM reported failure: {getattr(llm_response, 'info', '')}",
                info=int(Info.UNKNOWN_STATE),
                plan_id=plan_id,
                observations=observations_text,
                llm_elapsed_sec=llm_elapsed,
            )
        response_text = getattr(llm_response, "response", "")
        plan_text = extract_plan_text(response_text)
        if not plan_text:
            return TaskReport(
                TaskStatus.LLM_ERROR,
                "LLM response contained no plan text",
                info=int(Info.UNKNOWN_STATE),
                plan_id=plan_id,
                llm_elapsed_sec=llm_elapsed,
            )

        try:
            plan = parse_plan(
                plan_text,
                self.config.library,
                max_steps=self.config.max_steps,
                max_bytes=self.config.max_plan_bytes,
            )
        except PlanError as exc:
            return TaskReport(
                TaskStatus.PLAN_REJECTED,
                f"LLM plan rejected before execution: {exc}",
                info=int(exc.info),
                plan_id=plan_id,
                observations=observations_text,
                llm_elapsed_sec=llm_elapsed,
            )

        self.logger.info(
            f"{plan_id}: executing plan {[step.name for step in plan.steps]}"
        )
        exec_result = self.host.call(
            self.config.execute_service,
            ExecuteBehavior.Request(behavior=to_ndjson(plan)),
            self.config.execute_timeout_sec,
            request_id=f"{plan_id}:exec",
        )
        plan_dict = [
            {"name": step.name, "input": step.input_obj} for step in plan.steps
        ]
        if not exec_result.ok:
            status = (
                TaskStatus.EXECUTION_TIMEOUT
                if exec_result.status is CallStatus.TIMEOUT
                else TaskStatus.EXECUTOR_UNAVAILABLE
                if exec_result.status is CallStatus.UNAVAILABLE
                else TaskStatus.EXECUTION_UNKNOWN
            )
            return TaskReport(
                status=status,
                message=(
                    f"execute_behavior call failed ({exec_result.status.value}): "
                    f"{exec_result.error}. State is not confirmed; the plan was "
                    "not retried."
                ),
                info=int(Info.TIMEOUT)
                if exec_result.status is CallStatus.TIMEOUT
                else int(Info.UNKNOWN_STATE),
                plan_id=plan_id,
                plan=plan_dict,
                observations=observations_text,
                llm_elapsed_sec=llm_elapsed,
            )

        response = exec_result.response
        execution_report = parse_report(getattr(response, "message", ""))
        failure_status = self._execution_failure(response, execution_report)
        if failure_status is not None:
            return TaskReport(
                status=failure_status,
                message=(
                    execution_report.message
                    if execution_report is not None
                    else getattr(response, "message", "execution failed")
                ),
                info=int(getattr(response, "info", Info.UNKNOWN_STATE)),
                plan_id=plan_id,
                plan=plan_dict,
                execution=execution_report.to_dict() if execution_report else {
                    "success": bool(getattr(response, "success", False)),
                    "message": getattr(response, "message", ""),
                    "info": int(getattr(response, "info", 0)),
                },
                observations=observations_text,
                llm_elapsed_sec=llm_elapsed,
            )

        # Execution reported success.  Do not equate that with task success.
        verification, unverified, verification_error = self._verify_plan(
            plan, execution_report, obs_view
        )
        final_summary = observations_text
        if self.config.use_observations:
            post = self.host.call(
                self.config.observation_service,
                Observation.Request(),
                self.config.observation_timeout_sec,
                request_id=f"{plan_id}:obs-post",
            )
            if post.ok:
                final_summary = ObservationView.from_messages(
                    post.response.observation
                ).summary()

        if verification_error:
            status = TaskStatus.VERIFICATION_UNAVAILABLE
            message = verification_error
        elif any(not check.ok for check in verification):
            status = TaskStatus.POSTCONDITION_FAILED
            failed = [check for check in verification if not check.ok]
            message = "execution succeeded but observed effects do not match: " + "; ".join(
                f"{check.action}: {check.reason}" for check in failed
            )
        elif verification and not unverified:
            status = TaskStatus.SUCCESS_VERIFIED
            message = "task succeeded and every action effect was observed"
        elif verification:
            status = TaskStatus.SUCCESS_PARTIALLY_VERIFIED
            message = (
                "task executed; observed effects passed for verified actions, "
                f"unverified actions: {unverified}"
            )
        else:
            status = TaskStatus.SUCCESS_UNVERIFIED
            message = (
                "task executed without errors, but the action library defines no "
                "post-condition checks; task completion is not proven"
            )
        return TaskReport(
            status=status,
            message=message,
            info=int(Info.OK) if status.is_success else int(Info.NOT_VERIFIED),
            plan_id=plan_id,
            plan=plan_dict,
            execution=execution_report.to_dict() if execution_report else None,
            verification=[check.to_dict() for check in verification],
            unverified_actions=unverified,
            observations=final_summary,
            llm_elapsed_sec=llm_elapsed,
        )

    @staticmethod
    def _execution_failure(response, execution_report: ExecutionReport | None):
        if execution_report is not None:
            if execution_report.status is ExecStatus.OK:
                return None
            mapping = {
                ExecStatus.BUSY: TaskStatus.BUSY,
                ExecStatus.INVALID: TaskStatus.PLAN_REJECTED,
                ExecStatus.LIMIT: TaskStatus.PLAN_REJECTED,
                ExecStatus.TIMEOUT: TaskStatus.EXECUTION_TIMEOUT,
                ExecStatus.UNKNOWN: TaskStatus.EXECUTION_UNKNOWN,
                ExecStatus.UNAVAILABLE: TaskStatus.EXECUTOR_UNAVAILABLE,
                ExecStatus.FAILED: TaskStatus.EXECUTION_FAILED,
            }
            return mapping.get(execution_report.status, TaskStatus.EXECUTION_FAILED)
        if bool(getattr(response, "success", False)):
            return None
        info = int(getattr(response, "info", 0))
        if info == Info.BUSY:
            return TaskStatus.BUSY
        if info in (Info.NULL_BEHAVIOR, Info.INVALID_BEHAVIOR, Info.LIMIT_EXCEEDED):
            return TaskStatus.PLAN_REJECTED
        if info == Info.TIMEOUT:
            return TaskStatus.EXECUTION_TIMEOUT
        if info == Info.ATOMIC_ACTION_UNAVAILABLE:
            return TaskStatus.EXECUTOR_UNAVAILABLE
        if info == Info.UNKNOWN_STATE:
            return TaskStatus.EXECUTION_UNKNOWN
        return TaskStatus.EXECUTION_FAILED

    def _verify_plan(
        self,
        plan: Plan,
        execution_report: ExecutionReport | None,
        before_view: ObservationView,
    ) -> tuple[list[VerificationCheck], list[str], str]:
        checks: list[VerificationCheck] = []
        unverified = [step.name for step in plan.steps if step.spec.verify is None]
        if not any(step.spec.verify is not None for step in plan.steps):
            return checks, unverified, ""

        if execution_report is None:
            return (
                checks,
                unverified,
                "executor returned success without per-step observation snapshots; "
                "completed action effects cannot be verified",
            )
        by_index = {record.index: record for record in execution_report.steps}
        last: dict[str, Any] = {
            name: before_view.get(name)
            for name in {step.spec.verify.observation for step in plan.steps if step.spec.verify}
        }
        for step in plan.steps:
            verify = step.spec.verify
            if verify is None:
                continue
            record = by_index.get(step.index)
            if record is None:
                return (
                    checks,
                    unverified,
                    f"executor report has no result for step {step.index + 1}",
                )
            after = entry_from_snapshot(record.observation) if record.observation else None
            check = verify_effect(
                step.name,
                verify,
                last.get(verify.observation),
                after,
                max_age_sec=self.config.max_observation_age_sec,
                require_age=self.config.require_observation_age,
            )
            checks.append(check)
            if after is not None:
                last[verify.observation] = after
        return checks, unverified, ""
