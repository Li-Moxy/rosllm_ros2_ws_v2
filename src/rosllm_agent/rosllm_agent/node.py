"""ROS node wrapper around :class:`rosllm_agent.agent.Agent`."""

from __future__ import annotations

import math
from pathlib import Path

from rclpy.node import Node
from rclpy.parameter import Parameter
from rosllm_srvs.srv import ExecuteBehavior, LLM, Observation

from rosllm_core.action_client import ClientHost
from rosllm_core.library import ActionLibrary

from .agent import Agent, AgentConfig, TaskReport


class RosAgentNode(Node):
    def __init__(self, overrides: dict | None = None):
        parameter_overrides = [
            Parameter(key, value=value) for key, value in (overrides or {}).items()
        ]
        super().__init__("ros_agent", parameter_overrides=parameter_overrides)
        self._declare_parameters()
        self._validate_parameters()

        library_path = str(self.get_parameter("action_library_path").value).strip()
        if not library_path:
            raise RuntimeError(
                "parameter 'action_library_path' is required; use "
                "'--ros-args -p action_library_path:=/path/to/action_library.yaml'"
            )
        try:
            library = ActionLibrary.load(library_path)
        except Exception as exc:
            raise RuntimeError(f"cannot load action library: {exc}") from exc

        expert_advice = ""
        advice_path = str(self.get_parameter("expert_advice_path").value).strip()
        if advice_path:
            try:
                expert_advice = Path(advice_path).read_text(encoding="utf-8")
            except OSError as exc:
                self.get_logger().warning(
                    f"cannot read expert_advice_path {advice_path!r}: {exc}"
                )
            if len(expert_advice.encode("utf-8")) > 16384:
                self.get_logger().warning("expert advice is longer than 16 KiB; truncated")
                expert_advice = expert_advice[:16384]

        config = AgentConfig(
            library=library,
            llm_service=str(self.get_parameter("llm_service").value),
            execute_service=str(self.get_parameter("execute_service").value),
            observation_service=str(self.get_parameter("observation_service").value),
            max_steps=int(self.get_parameter("max_steps").value),
            max_plan_bytes=int(self.get_parameter("max_plan_bytes").value),
            llm_timeout_sec=float(self.get_parameter("llm_timeout_sec").value),
            execute_timeout_sec=float(self.get_parameter("execute_timeout_sec").value),
            observation_timeout_sec=float(
                self.get_parameter("observation_timeout_sec").value
            ),
            max_observation_age_sec=float(
                self.get_parameter("max_observation_age_sec").value
            ),
            require_observation_age=bool(
                self.get_parameter("require_observation_age").value
            ),
            require_fresh_observation=bool(
                self.get_parameter("require_fresh_observation").value
            ),
            use_observations=bool(self.get_parameter("use_observations").value),
            expert_advice=expert_advice,
            max_task_bytes=int(self.get_parameter("max_task_bytes").value),
        )
        self.host = ClientHost("ros_agent_clients", num_threads=2)
        for service, srv_type in (
            (config.llm_service, LLM),
            (config.execute_service, ExecuteBehavior),
            (config.observation_service, Observation),
        ):
            self.host.create_client(service, srv_type)
        self.agent = Agent(config, self.host, self.get_logger())
        self.get_logger().info(
            "ros_agent ready: actions=%s llm=%s executor=%s observations=%s"
            % (
                list(library.names),
                config.llm_service,
                config.execute_service,
                config.observation_service,
            )
        )

    def _declare_parameters(self) -> None:
        self.declare_parameter("action_library_path", "")
        self.declare_parameter("llm_service", "/call_llm")
        self.declare_parameter("execute_service", "/execute_behavior")
        self.declare_parameter("observation_service", "/get_observation")
        self.declare_parameter("max_steps", 8)
        self.declare_parameter("max_plan_bytes", 16384)
        self.declare_parameter("llm_timeout_sec", 60.0)
        self.declare_parameter("execute_timeout_sec", 95.0)
        self.declare_parameter("observation_timeout_sec", 2.0)
        self.declare_parameter("max_observation_age_sec", 3.0)
        self.declare_parameter("require_observation_age", False)
        self.declare_parameter("require_fresh_observation", False)
        self.declare_parameter("use_observations", True)
        self.declare_parameter("expert_advice_path", "")
        self.declare_parameter("max_task_bytes", 4096)

    def _validate_parameters(self) -> None:
        def finite(name: str, low: float, high: float) -> float:
            value = float(self.get_parameter(name).value)
            if not math.isfinite(value) or not low <= value <= high:
                raise RuntimeError(f"parameter {name!r} must be within {low}..{high}")
            return value

        finite("llm_timeout_sec", 0.5, 600.0)
        finite("execute_timeout_sec", 1.0, 3600.0)
        finite("observation_timeout_sec", 0.1, 60.0)
        finite("max_observation_age_sec", 0.1, 3600.0)
        max_steps = int(self.get_parameter("max_steps").value)
        if not 1 <= max_steps <= 64:
            raise RuntimeError("parameter 'max_steps' must be within 1..64")
        max_plan_bytes = int(self.get_parameter("max_plan_bytes").value)
        if not 256 <= max_plan_bytes <= 1024 * 1024:
            raise RuntimeError("parameter 'max_plan_bytes' must be within 256..1048576")
        max_task_bytes = int(self.get_parameter("max_task_bytes").value)
        if not 1 <= max_task_bytes <= 65536:
            raise RuntimeError("parameter 'max_task_bytes' must be within 1..65536")
        for name in ("llm_service", "execute_service", "observation_service"):
            value = str(self.get_parameter(name).value).strip()
            if not value:
                raise RuntimeError(f"parameter {name!r} must not be empty")

    def run_task(self, task: str) -> TaskReport:
        return self.agent.run_task(task)

    def shutdown(self) -> None:
        self.host.shutdown()
