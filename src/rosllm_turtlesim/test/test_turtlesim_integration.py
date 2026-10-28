import json
import os
import random
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest
import rclpy
from ament_index_python.packages import get_package_share_directory
from rosllm_msgs.msg import Observation as ObservationMsg
from rosllm_srvs.srv import AtomicAction, ExecuteBehavior, LLM, Observation
from std_msgs.msg import String

from rosllm_core.action_client import ClientHost
from rosllm_core.observation import ObservationView
from rosllm_core.library import ActionLibrary
from rosllm_core.plan import PlanError, parse_plan

os.environ["ROS_DOMAIN_ID"] = str(random.randint(10, 220))
os.environ["ROS_LOCALHOST_ONLY"] = "1"
DOMAIN = os.environ["ROS_DOMAIN_ID"]


@pytest.fixture(scope="module", autouse=True)
def ros_context():
    rclpy.init()
    yield
    rclpy.shutdown()


class Demo:
    def __init__(self):
        self.log_path = Path(f"/tmp/rosllm_turtle_demo_{uuid.uuid4().hex[:8]}.log")
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                "ros2",
                "launch",
                "rosllm_turtlesim",
                "turtlesim_demo.launch.py",
                "headless:=true",
                "robot_id:=turtlesim_test",
            ],
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )
        self.services = {
            "/execute_behavior": ExecuteBehavior,
            "/get_observation": Observation,
            "/call_llm": LLM,
            "/turtle1/forward": AtomicAction,
            "/turtle1/turn": AtomicAction,
            "/turtle1/command": AtomicAction,
        }
        self.host = ClientHost(f"turtle_test_{uuid.uuid4().hex[:8]}", num_threads=4)
        for name, srv_type in self.services.items():
            self.host.create_client(name, srv_type)

    def wait_ready(self, timeout=30.0):
        for name in self.services:
            if not self.host.wait_ready(name, timeout):
                self.log.flush()
                raise AssertionError(
                    f"service {name} did not appear; log:\n{self.log_path.read_text()}"
                )
        # The observation manager needs a first pose before the agent starts.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            result = self.host.call("/get_observation", Observation.Request(), 2.0)
            if result.ok:
                view = ObservationView.from_messages(result.response.observation)
                if view.status("turtle_pose") == "fresh":
                    return
            time.sleep(0.1)
        raise AssertionError("no fresh turtle_pose before timeout")

    def stop(self):
        if self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    self.proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.proc.wait(timeout=5.0)
        self.host.shutdown()
        self.log.close()


@pytest.fixture(scope="module")
def demo(ros_context):
    instance = Demo()
    try:
        instance.wait_ready()
        yield instance
    except Exception:
        instance.log.flush()
        raise
    finally:
        instance.stop()


def run_agent(share):
    completed = subprocess.run(
        [
            "ros2",
            "run",
            "rosllm_agent",
            "ros_agent",
            "--task",
            "Move forward, turn left, then move forward again",
            "--json",
            "--library",
            os.path.join(share, "config", "action_library.yaml"),
            "--expert-advice",
            os.path.join(share, "config", "expert_advice.txt"),
            "--ros-args",
            "-p",
            "max_observation_age_sec:=2.0",
            "-p",
            "require_observation_age:=true",
            "-p",
            "observation_timeout_sec:=5.0",
            "-p",
            "llm_timeout_sec:=10.0",
            "-p",
            "execute_timeout_sec:=70.0",
        ],
        capture_output=True,
        text=True,
        timeout=60.0,
        env=os.environ.copy(),
        start_new_session=True,
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    return completed, json.loads(lines[-1]) if lines else None


def test_full_offline_pipeline_and_action_validation(demo):
    share = get_package_share_directory("rosllm_turtlesim")
    before = demo.host.call("/get_observation", Observation.Request(), 5.0)
    before_view = ObservationView.from_messages(before.response.observation)
    x0 = before_view.payload("turtle_pose")["x"]

    completed, report = run_agent(share)
    assert completed.returncode == 0, (completed.stdout + "\n" + completed.stderr)
    assert report is not None, completed.stdout
    assert report["status"] == "success_verified", report
    assert report["exit_code"] == 0
    assert [step["name"] for step in report["plan"]] == [
        "forward",
        "turn",
        "forward",
    ]
    deltas = [item["delta"] for item in report["verification"]]
    assert len(deltas) == 3
    assert deltas[0] > 0.5
    assert abs(deltas[1]) > 0.5
    assert report["execution"]["completed_steps"] == 3

    after = demo.host.call("/get_observation", Observation.Request(), 5.0)
    after_view = ObservationView.from_messages(after.response.observation)
    assert after_view.status("turtle_pose") == "fresh"
    payload = after_view.payload("turtle_pose")
    assert payload["x"] > x0 + 0.5
    assert abs(payload["theta"]) > 0.5

    # Invalid atomic-action inputs are rejected without moving the turtle.
    def call_action(name, text):
        result = demo.host.call(
            name, AtomicAction.Request(input=text), 5.0, request_id=f"invalid-{name}"
        )
        assert result.ok, result.error
        return result.response

    assert not call_action("/turtle1/forward", '{"vel": 5.0}').success
    assert not call_action("/turtle1/forward", '{"vel": NaN}').success
    assert not call_action("/turtle1/forward", '{"vel": 1.0, "extra": 1}').success
    assert not call_action(
        "/turtle1/command",
        '{"fwd_vel": 1.0, "strafe_vel": 0.5, "ang_vel": 0.0}',
    ).success
    assert not call_action("/turtle1/forward", "not json").success

    still_ok = demo.host.call("/get_observation", Observation.Request(), 5.0)
    assert still_ok.ok and still_ok.response.observation


def test_nonzero_strafe_is_rejected_before_any_plan_action(demo):
    share = Path(get_package_share_directory("rosllm_turtlesim"))
    library = ActionLibrary.load(share / "config/action_library.yaml")
    behavior = ('{"name":"forward","input":{"vel":1}}\n'
                '{"name":"command","input":{"fwd_vel":1,"strafe_vel":1,"ang_vel":0}}')
    with pytest.raises(PlanError):
        parse_plan(behavior, library)
    result = demo.host.call("/execute_behavior", ExecuteBehavior.Request(behavior=behavior), 5.0)
    assert result.ok and result.response.info == -7
    report = json.loads(result.response.message)
    assert report["completed_steps"] == 0 and report["steps"] == []
