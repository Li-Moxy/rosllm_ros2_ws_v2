import json
import os
import random
import signal
import subprocess
import threading
import time
import uuid
from functools import partial
from pathlib import Path

import pytest
import rclpy
import yaml
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_msgs.msg import LLMResponse, Observation as ObservationMsg
from rosllm_srvs.srv import AtomicAction, LLM, Observation

from rosllm_core.action_client import ClientHost
from rosllm_core.jsonutil import dumps, loads
from rosllm_core.observation import META_PREFIX
from rosllm_srvs.srv import ExecuteBehavior

os.environ["ROS_DOMAIN_ID"] = str(random.randint(10, 220))
os.environ["ROS_LOCALHOST_ONLY"] = "1"


@pytest.fixture(scope="module", autouse=True)
def ros_context():
    rclpy.init()
    yield
    rclpy.shutdown()


def make_library(path: Path, prefix: str) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "actions": {
                    "forward": {
                        "service": f"/{prefix}/test/forward",
                        "description": "Move forward.",
                        "parameters": {
                            "vel": {"type": "number", "min": -5, "max": 5}
                        },
                        "verify": {
                            "observation": "pose",
                            "field": "x",
                            "min_abs_delta": 0.05,
                        },
                    },
                    "turn": {
                        "service": f"/{prefix}/test/turn",
                        "description": "Turn.",
                        "parameters": {
                            "vel": {"type": "number", "min": -5, "max": 5}
                        },
                        "verify": {
                            "observation": "pose",
                            "field": "theta",
                            "min_abs_delta": 0.05,
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class DummyWorld(Node):
    def __init__(self, prefix):
        super().__init__(f"agent_dummy_world_{uuid.uuid4().hex[:8]}")
        self.prefix = prefix
        self.lock = threading.Lock()
        self.calls = 0
        self.x = 0.0
        self.theta = 0.0
        self.no_effect = False
        self.obs_mode = "fresh"
        self.seq = 0
        group = ReentrantCallbackGroup()
        for name in ("forward", "turn"):
            self.create_service(
                AtomicAction,
                f"/{prefix}/test/{name}",
                partial(self._action, name=name),
                callback_group=group,
            )
        self.create_service(
            Observation, f"/{prefix}/get_observation", self._observation, callback_group=group
        )

    def _action(self, request, response, name):
        data = loads(request.input) if request.input.strip() else {}
        with self.lock:
            self.calls += 1
            if not self.no_effect:
                if name == "forward":
                    self.x += float(data.get("vel", 0.0))
                else:
                    self.theta += float(data.get("vel", 0.0))
            payload = dumps({"x": self.x, "theta": self.theta})
        response.success = True
        response.output = payload
        return response

    def _observation(self, request, response):
        with self.lock:
            self.seq += 1
            payload = dumps({"x": self.x, "theta": self.theta})
            mode = self.obs_mode
            seq = self.seq
        message = ObservationMsg()
        message.name = "pose"
        if mode == "stale":
            message.text = [
                payload,
                META_PREFIX
                + json.dumps({"status": "stale", "age_sec": 10.0, "seq": seq}),
            ]
        elif mode == "missing":
            message.text = [
                META_PREFIX + json.dumps({"status": "missing", "seq": seq})
            ]
        else:
            message.text = [
                payload,
                META_PREFIX
                + json.dumps({"status": "fresh", "age_sec": 0.0, "seq": seq}),
            ]
        response.observation = [message]
        return response


class FakeLLM(Node):
    def __init__(self, prefix):
        super().__init__(f"fake_llm_{uuid.uuid4().hex[:8]}")
        self.prefix = prefix
        self.text = ""
        self.success = True
        self.calls = 0
        self.lock = threading.Lock()
        self.create_service(LLM, f"/{prefix}/call_llm", self._handle)

    def _handle(self, request, response):
        with self.lock:
            self.calls += 1
            text = self.text
            success = self.success
        message = LLMResponse()
        message.header.stamp = self.get_clock().now().to_msg()
        message.req = self.calls
        message.success = success
        message.info = "fake" if success else "fake failure"
        message.response = text
        response.response = message
        return response


class NodeHost:
    def __init__(self, node):
        self.node = node
        self.executor = MultiThreadedExecutor(num_threads=6)
        self.executor.add_node(node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    def stop(self):
        self.executor.shutdown(timeout_sec=3.0)
        self.thread.join(timeout=3.0)
        self.node.destroy_node()


class ExecutorProc:
    def __init__(self, tmp_path, library, robot_id, prefix):
        cmd = [
            "ros2",
            "run",
            "rosllm_executor",
            "sequence_executor",
            "--ros-args",
            "-p",
            f"action_library_path:={library}",
            "-p",
            f"robot_id:={robot_id}",
            "-p",
            f"service_name:=/{prefix}/execute_behavior",
            "-p",
            f"observation_service:=/{prefix}/get_observation",
            "-p",
            "step_timeout_sec:=2.0",
            "-p",
            "plan_timeout_sec:=15.0",
            "-p",
            "preflight_timeout_sec:=1.0",
            "-p",
            "observation_settle_sec:=0.02",
        ]
        self.prefix = prefix
        self.log_path = tmp_path / f"executor-{uuid.uuid4().hex[:6]}.log"
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )

    def wait_ready(self, timeout=25.0):
        probe = ClientHost(f"agent_probe_{uuid.uuid4().hex[:8]}")
        try:
            service = f"/{self.prefix}/execute_behavior"
            probe.create_client(service, ExecuteBehavior)
            ready = probe.wait_ready(service, timeout)
        finally:
            probe.shutdown()
        if self.proc.poll() is not None:
            self.log.flush()
            raise AssertionError(f"executor died:\n{self.log_path.read_text()}")
        if not ready:
            self.log.flush()
            raise AssertionError(f"executor not ready:\n{self.log_path.read_text()}")

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=8.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait(timeout=5.0)
        self.log.close()


@pytest.fixture()
def prefix():
    return f"t{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def world(prefix):
    node = DummyWorld(prefix)
    host = NodeHost(node)
    try:
        yield node
    finally:
        host.stop()


@pytest.fixture()
def fake_llm(prefix):
    node = FakeLLM(prefix)
    host = NodeHost(node)
    try:
        yield node
    finally:
        host.stop()


def run_agent(tmp_path, library, task, prefix, extra_args=()):
    cmd = [
        "ros2",
        "run",
        "rosllm_agent",
        "ros_agent",
        "--task",
        task,
        "--json",
        "--library",
        str(library),
        "--llm-service",
        f"/{prefix}/call_llm",
        "--execute-service",
        f"/{prefix}/execute_behavior",
        "--observation-service",
        f"/{prefix}/get_observation",
        "--ros-args",
        "-p",
        "max_observation_age_sec:=2.0",
        "-p",
        "require_observation_age:=true",
        "-p",
        "llm_timeout_sec:=5.0",
        "-p",
        "execute_timeout_sec:=20.0",
        "-p",
        "observation_timeout_sec:=5.0",
        *extra_args,
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=40.0,
        env=os.environ.copy(),
        start_new_session=True,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    report = json.loads(lines[-1]) if lines else None
    return completed, report


PLAN = """```json
{"name": "forward", "input": {"vel": 1.0}}
{"name": "turn", "input": {"vel": 0.5}}
```"""


def test_success_is_verified_from_observed_effects(tmp_path, world, fake_llm, prefix):
    library = make_library(tmp_path / "lib.yaml", prefix)
    executor = ExecutorProc(tmp_path, library, "agent-robot", prefix)
    fake_llm.text = PLAN
    try:
        executor.wait_ready()
        completed, report = run_agent(tmp_path, library, "move forward then turn", prefix)
        assert completed.returncode == 0, completed.stderr
        assert report["status"] == "success_verified"
        assert report["exit_code"] == 0
        assert report["plan"] == [
            {"name": "forward", "input": {"vel": 1.0}},
            {"name": "turn", "input": {"vel": 0.5}},
        ]
        assert [v["ok"] for v in report["verification"]] == [True, True]
        assert report["verification"][0]["delta"] == pytest.approx(1.0)
        assert report["verification"][1]["delta"] == pytest.approx(0.5)
        assert report["execution"]["completed_steps"] == 2
        assert world.calls == 2
    finally:
        executor.stop()


def test_success_without_observed_change_is_not_reported_as_success(
    tmp_path, world, fake_llm, prefix
):
    library = make_library(tmp_path / "lib.yaml", prefix)
    executor = ExecutorProc(tmp_path, library, "agent-robot", prefix)
    fake_llm.text = PLAN
    world.no_effect = True
    try:
        executor.wait_ready()
        completed, report = run_agent(tmp_path, library, "move", prefix)
        assert completed.returncode == 2
        assert report["status"] == "postcondition_failed", report
        assert all(not v["ok"] for v in report["verification"])
        assert world.calls == 2  # no automatic retry of the plan
    finally:
        executor.stop()


@pytest.mark.parametrize("mode", ["stale", "missing"])
def test_stale_or_missing_observation_blocks_planning(
    tmp_path, world, fake_llm, prefix, mode
):
    library = make_library(tmp_path / "lib.yaml", prefix)
    executor = ExecutorProc(tmp_path, library, "agent-robot", prefix)
    fake_llm.text = PLAN
    world.obs_mode = mode
    try:
        executor.wait_ready()
        completed, report = run_agent(tmp_path, library, "move", prefix)
        assert completed.returncode == 2
        assert report["status"] == "observation_unavailable", report
        assert world.calls == 0
        assert fake_llm.calls == 0
    finally:
        executor.stop()


def test_empty_llm_response_is_a_structured_error(tmp_path, world, fake_llm, prefix):
    library = make_library(tmp_path / "lib.yaml", prefix)
    executor = ExecutorProc(tmp_path, library, "agent-robot", prefix)
    fake_llm.text = "   "
    try:
        executor.wait_ready()
        completed, report = run_agent(tmp_path, library, "move", prefix)
        assert completed.returncode == 2
        assert report["status"] == "llm_error", report
        assert world.calls == 0
    finally:
        executor.stop()


def test_invalid_llm_plan_is_rejected_before_execution(tmp_path, world, fake_llm, prefix):
    library = make_library(tmp_path / "lib.yaml", prefix)
    executor = ExecutorProc(tmp_path, library, "agent-robot", prefix)
    fake_llm.text = '```json\n{"name": "evil", "input": {}}\n```'
    try:
        executor.wait_ready()
        completed, report = run_agent(tmp_path, library, "do something unsafe", prefix)
        assert completed.returncode == 2
        assert report["status"] == "plan_rejected", report
        assert world.calls == 0
    finally:
        executor.stop()


def test_llm_service_unavailable_is_reported(tmp_path, world, prefix):
    library = make_library(tmp_path / "lib.yaml", prefix)
    executor = ExecutorProc(tmp_path, library, "agent-robot", prefix)
    try:
        executor.wait_ready()
        completed, report = run_agent(tmp_path, library, "move", prefix)
        assert completed.returncode == 2
        assert report["status"] == "llm_error", report
        assert world.calls == 0
    finally:
        executor.stop()
