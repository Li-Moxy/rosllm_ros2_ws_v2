"""Shared executor infrastructure: parameters, lease, preflight, snapshots."""

from __future__ import annotations

import math
import os
import signal
import threading
import time
from pathlib import Path

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_srvs.srv import AtomicAction, ExecuteBehavior, Observation
from std_srvs.srv import Trigger

from rosllm_core.action_client import CallStatus, ClientHost
from rosllm_core.info import Info
from rosllm_core.lease import LeaseError, PlanLease
from rosllm_core.library import ActionLibrary
from rosllm_core.observation import ObservationView
from rosllm_core.plan import PlanError
from rosllm_core.bt import TreeError
from rosllm_core.report import (
    ExecStatus,
    ExecutionReport,
    StepRecord,
    report_json,
    status_for_info,
)


class ExecutorBase(Node):
    """One ExecuteBehavior service with plan-level mutual exclusion."""

    def __init__(self, node_name: str):
        super().__init__(node_name)
        self._declare_common_parameters()
        self.library = self._load_library()
        self.step_timeout = float(self.get_parameter("step_timeout_sec").value)
        self.plan_timeout = float(self.get_parameter("plan_timeout_sec").value)
        self.max_steps = int(self.get_parameter("max_steps").value)
        self.max_plan_bytes = int(self.get_parameter("max_plan_bytes").value)
        self.max_step_input_bytes = int(self.get_parameter("max_step_input_bytes").value)
        self.preflight_timeout = float(self.get_parameter("preflight_timeout_sec").value)
        self.observation_service = str(
            self.get_parameter("observation_service").value
        ).strip()
        self.observation_timeout = float(
            self.get_parameter("observation_timeout_sec").value
        )
        self.observation_settle = float(
            self.get_parameter("observation_settle_sec").value
        )
        self.enable_snapshots = bool(
            self.get_parameter("enable_observation_snapshots").value
        )
        self.robot_id = str(self.get_parameter("robot_id").value)
        lease_dir = str(self.get_parameter("lease_dir").value)
        service_name = str(self.get_parameter("service_name").value)

        self._validate_parameters()
        try:
            self.lease = PlanLease(self.robot_id, lease_dir)
        except LeaseError as exc:
            raise RuntimeError(f"cannot initialise plan lease: {exc}") from exc

        self.host = ClientHost(f"{node_name}_clients", num_threads=3)
        for service in self.library.services:
            self.host.create_client(service, AtomicAction)
        if self.observation_service and self.enable_snapshots:
            self.host.create_client(self.observation_service, Observation)

        self._counter = 0
        self._counter_lock = threading.Lock()
        self._callback_group = ReentrantCallbackGroup()
        self._srv = self.create_service(
            ExecuteBehavior,
            service_name,
            self._handle,
            callback_group=self._callback_group,
        )
        self._recovery_srv = self.create_service(
            Trigger, service_name.rstrip("/") + "/confirm_stopped",
            self._confirm_stopped, callback_group=self._callback_group,
        )
        self.get_logger().info(
            "execute_behavior ready: actions=%s services=%s robot_id=%r lease=%s"
            % (
                list(self.library.names),
                list(self.library.services),
                self.robot_id,
                self.lease.path,
            )
        )

    # ------------------------------------------------------------ parameters
    def _declare_common_parameters(self) -> None:
        self.declare_parameter("action_library_path", "")
        self.declare_parameter("robot_id", "default")
        self.declare_parameter("lease_dir", "/tmp")
        self.declare_parameter("service_name", "execute_behavior")
        self.declare_parameter("step_timeout_sec", 30.0)
        self.declare_parameter("plan_timeout_sec", 90.0)
        self.declare_parameter("max_steps", 8)
        self.declare_parameter("max_plan_bytes", 16384)
        self.declare_parameter("max_step_input_bytes", 4096)
        self.declare_parameter("preflight_timeout_sec", 2.0)
        self.declare_parameter("observation_service", "get_observation")
        self.declare_parameter("observation_timeout_sec", 1.0)
        self.declare_parameter("observation_settle_sec", 0.15)
        self.declare_parameter("enable_observation_snapshots", True)

    def _load_library(self) -> ActionLibrary:
        path = str(self.get_parameter("action_library_path").value).strip()
        if not path:
            raise RuntimeError(
                "parameter 'action_library_path' is required; point it at the "
                "same YAML file used by the agent"
            )
        try:
            return ActionLibrary.load(Path(path))
        except Exception as exc:
            raise RuntimeError(f"cannot load action library: {exc}") from exc

    def _validate_parameters(self) -> None:
        def finite(name: str, value: float, low: float, high: float) -> None:
            if not math.isfinite(value) or not low <= value <= high:
                raise RuntimeError(
                    f"parameter {name!r} must be within {low}..{high}, got {value}"
                )

        finite("step_timeout_sec", self.step_timeout, 0.05, 3600.0)
        finite("plan_timeout_sec", self.plan_timeout, 0.1, 7200.0)
        finite("preflight_timeout_sec", self.preflight_timeout, 0.0, 60.0)
        finite("observation_timeout_sec", self.observation_timeout, 0.05, 30.0)
        finite("observation_settle_sec", self.observation_settle, 0.0, 5.0)
        if not 1 <= self.max_steps <= 64:
            raise RuntimeError("parameter 'max_steps' must be within 1..64")
        if not 256 <= self.max_plan_bytes <= 1024 * 1024:
            raise RuntimeError("parameter 'max_plan_bytes' must be within 256..1048576")
        if not 128 <= self.max_step_input_bytes <= 65536:
            raise RuntimeError(
                "parameter 'max_step_input_bytes' must be within 128..65536"
            )
        if not self.robot_id or len(self.robot_id) > 128:
            raise RuntimeError("parameter 'robot_id' must be 1..128 characters")
        if self.observation_service and not self.observation_service.startswith("/"):
            self.observation_service = "/" + self.observation_service

    # --------------------------------------------------------------- service
    def _handle(self, request, response):
        with self._counter_lock:
            self._counter += 1
            plan_id = f"{self.get_name()}-{os.getpid()}-{self._counter}"
        started = time.monotonic()
        self.get_logger().info(
            f"request {plan_id}: received behavior ({len(request.behavior)} bytes)"
        )

        try:
            lease = self.lease.try_acquire(
                owner=f"{self.get_name()}:{os.getpid()}", note=plan_id
            )
        except LeaseError as exc:
            report = ExecutionReport(
                status=ExecStatus.UNKNOWN,
                info=int(Info.UNKNOWN_STATE),
                message=f"plan lease error: {exc}",
                plan_id=plan_id,
                robot_id=self.robot_id,
            )
            self.get_logger().error(f"request {plan_id}: {report.message}")
            return self._respond(response, report)
        if not lease.granted:
            holder = lease.holder or {}
            if lease.reason == "recovery_required":
                return self._respond(response, ExecutionReport(
                    status=ExecStatus.UNKNOWN,
                    info=int(Info.UNKNOWN_STATE),
                    message=("previous action state is unconfirmed: "
                             f"{holder.get('reason', '')}; confirm the robot is stopped "
                             "before calling the confirm_stopped service"),
                    plan_id=plan_id, robot_id=self.robot_id,
                ))
            report = ExecutionReport(
                status=ExecStatus.BUSY,
                info=int(Info.BUSY),
                message=(
                    f"another plan is already executing (holder="
                    f"{holder.get('owner', 'unknown')}, note={holder.get('note', '')}); "
                    "request rejected without executing any action"
                ),
                plan_id=plan_id,
                robot_id=self.robot_id,
            )
            return self._respond(response, report)

        report = None
        try:
            try:
                parsed = self._parse(request.behavior)
                services = self._services_for(parsed)
            except (PlanError, TreeError) as exc:
                report = ExecutionReport(
                    status=status_for_info(int(exc.info)),
                    info=int(exc.info),
                    message=str(exc),
                    plan_id=plan_id,
                    robot_id=self.robot_id,
                )
                self.get_logger().warning(f"request {plan_id}: {exc}")
                return self._respond(response, report)

            deadline = time.monotonic() + self.plan_timeout
            missing = self._preflight(services, deadline)
            if missing:
                report = ExecutionReport(
                    status=ExecStatus.UNAVAILABLE,
                    info=int(Info.ATOMIC_ACTION_UNAVAILABLE),
                    message=(
                        "atomic action service(s) unavailable: "
                        + ", ".join(missing)
                        + "; no action was executed"
                    ),
                    plan_id=plan_id,
                    robot_id=self.robot_id,
                    total_steps=self._total_steps(parsed),
                )
                self.get_logger().error(f"request {plan_id}: {report.message}")
                return self._respond(response, report)

            report = self._execute(parsed, plan_id, deadline)
        except Exception as exc:  # noqa: BLE001 - never let the node exit
            self.get_logger().error(
                f"request {plan_id}: internal error {type(exc).__name__}: {exc}"
            )
            report = ExecutionReport(
                status=ExecStatus.UNKNOWN,
                info=int(Info.UNKNOWN_STATE),
                message=f"internal executor error: {type(exc).__name__}: {exc}",
                plan_id=plan_id,
                robot_id=self.robot_id,
                elapsed_sec=time.monotonic() - started,
            )
        finally:
            try:
                if report is not None and report.status in (ExecStatus.TIMEOUT, ExecStatus.UNKNOWN):
                    self.lease.mark_uncertain(report.message)
            except LeaseError as exc:
                # Keep the lock if persistence fails; never silently admit a new plan.
                self.get_logger().error(str(exc))
            else:
                self.lease.release()

        report.elapsed_sec = time.monotonic() - started
        return self._respond(response, report)

    def _confirm_stopped(self, request, response):
        try:
            result = self.lease.confirm_stopped(self.get_name())
            response.success = result.granted
            response.message = result.reason
        except (LeaseError, OSError) as exc:
            response.success = False
            response.message = str(exc)
        return response

    def _respond(self, response, report: ExecutionReport):
        response.success = report.success
        response.message = report_json(report)
        response.info = int(report.info)
        log = self.get_logger()
        if report.success:
            log.info(
                f"request {report.plan_id}: ok in {report.elapsed_sec:.2f}s "
                f"({report.completed_steps}/{report.total_steps} steps)"
            )
        else:
            log.warning(
                f"request {report.plan_id}: {report.status.value} "
                f"info={report.info} in {report.elapsed_sec:.2f}s: {report.message}"
            )
        return response

    # ------------------------------------------------------------- utilities
    def _preflight(self, services, deadline: float) -> list[str]:
        missing = []
        for service in services:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                missing.append(service)
                continue
            if not self.host.wait_ready(
                service, min(self.preflight_timeout, remaining)
            ):
                missing.append(service)
        return missing

    def _snapshot(self, plan_id: str, step: StepRecord, observation_name: str, deadline: float):
        if not self.enable_snapshots or not self.observation_service:
            return None
        if not observation_name:
            return None
        if self.observation_settle > 0:
            settle = min(self.observation_settle, max(0.0, deadline - time.monotonic()))
            if settle > 0:
                time.sleep(settle)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "name": observation_name,
                "status": "error",
                "detail": "plan deadline expired before the observation snapshot",
            }
        timeout = min(self.observation_timeout, remaining)
        result = self.host.call(
            self.observation_service,
            Observation.Request(),
            timeout,
            request_id=f"{plan_id}:obs{step.index}",
        )
        if not result.ok:
            return {
                "name": observation_name,
                "status": "error",
                "detail": f"{result.status.value}: {result.error}",
            }
        view = ObservationView.from_messages(result.response.observation)
        entry = view.get(observation_name)
        if entry is None:
            return {
                "name": observation_name,
                "status": "missing",
                "detail": "observation manager did not return this observation",
            }
        return entry.snapshot()

    # --------------------------------------------------------- subclass hooks
    def _parse(self, behavior: str):
        raise NotImplementedError

    def _services_for(self, parsed) -> list[str]:
        raise NotImplementedError

    def _total_steps(self, parsed) -> int:
        raise NotImplementedError

    def _execute(self, parsed, plan_id: str, deadline: float) -> ExecutionReport:
        raise NotImplementedError

    def shutdown(self) -> None:
        self.lease.release()
        self.host.shutdown()


def spin_node(node_cls, *, num_threads: int = 4):
    rclpy.init()
    node = None
    executor = None
    try:
        node = node_cls()
        executor = MultiThreadedExecutor(num_threads=num_threads)
        executor.add_node(node)
        executor.spin()
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    except Exception:
        if rclpy.ok():
            raise
    finally:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        if executor is not None:
            try:
                executor.shutdown(timeout_sec=5.0)
            except BaseException:
                pass
        if node is not None:
            try:
                node.shutdown()
            except BaseException:
                pass
            try:
                node.destroy_node()
            except BaseException:
                pass
        if rclpy.ok():
            rclpy.shutdown()
