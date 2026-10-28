import json
import os
import random
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pytest
import rclpy
import yaml
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_msgs.msg import Observation as ObservationMsg
from rosllm_srvs.srv import AtomicAction, ExecuteBehavior, Observation
from std_srvs.srv import Trigger

from rosllm_core.action_client import ClientHost
from rosllm_core.jsonutil import dumps, loads
from rosllm_core.observation import META_PREFIX
from rosllm_core.report import ExecStatus, parse_report

os.environ["ROS_DOMAIN_ID"] = str(random.randint(10, 220))
os.environ["ROS_LOCALHOST_ONLY"] = "1"


@pytest.fixture(scope="module", autouse=True)
def ros_context():
    rclpy.init()
    yield
    rclpy.shutdown()


def make_library(path: Path) -> Path:
    data = {
        "version": 1,
        "actions": {
            "forward": {
                "service": "/test/forward",
                "description": "Move forward.",
                "parameters": {"vel": {"type": "number", "min": -5, "max": 5}},
                "verify": {
                    "observation": "pose",
                    "field": "x",
                    "min_abs_delta": 0.01,
                },
            },
            "turn": {
                "service": "/test/turn",
                "description": "Turn.",
                "parameters": {"vel": {"type": "number", "min": -5, "max": 5}},
                "verify": {
                    "observation": "pose",
                    "field": "theta",
                    "min_abs_delta": 0.01,
                },
            },
            "slow": {
                "service": "/test/slow",
                "description": "Slow move.",
                "parameters": {"vel": {"type": "number", "min": -5, "max": 5}},
                "verify": {
                    "observation": "pose",
                    "field": "x",
                    "min_abs_delta": 0.01,
                },
            },
            "fail": {"service": "/test/fail", "description": "Always fails."},
            "missing": {"service": "/test/missing", "description": "Not served."},
        },
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class DummyWorld(Node):
    def __init__(self):
        super().__init__(f"dummy_world_{uuid.uuid4().hex[:8]}")
        self.lock = threading.Lock()
        self.calls = []
        self.x = 0.0
        self.theta = 0.0
        self.delay = 0.0
        self.fail_on = set()
        self.obs_mode = "fresh"
        self.seq = 0
        group = ReentrantCallbackGroup()
        for name in ("forward", "turn", "slow", "fail"):
            self.create_service(
                AtomicAction,
                f"/test/{name}",
                partial(self._action, name=name),
                callback_group=group,
            )
        self.create_service(
            Observation, "/get_observation", self._observation, callback_group=group
        )

    def _action(self, request, response, name):
        with self.lock:
            self.calls.append(
                {
                    "name": name,
                    "input": request.input,
                    "prev": request.prev_output,
                    "at": time.monotonic(),
                }
            )
            delay = self.delay
            fail = name in self.fail_on
        if delay:
            time.sleep(delay)
        if fail:
            response.success = False
            response.output = "deliberate failure"
            return response
        data = loads(request.input) if request.input.strip() else {}
        with self.lock:
            if name in ("forward", "slow"):
                self.x += float(data.get("vel", 0.0))
            elif name == "turn":
                self.theta += float(data.get("vel", 0.0))
            payload = dumps({"x": self.x, "theta": self.theta})
            self.calls[-1]["output"] = payload
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
        if mode == "missing":
            message.text = [
                META_PREFIX + json.dumps({"status": "missing", "detail": "never received"})
            ]
        elif mode == "stale":
            message.text = [
                payload,
                META_PREFIX
                + json.dumps({"status": "stale", "age_sec": 10.0, "seq": seq}),
            ]
        elif mode == "error":
            message.text = [
                META_PREFIX + json.dumps({"status": "error", "detail": "sensor error"})
            ]
        else:
            message.text = [
                payload,
                META_PREFIX
                + json.dumps({"status": "fresh", "age_sec": 0.0, "seq": seq}),
            ]
        response.observation = [message]
        return response

    def call_count(self):
        with self.lock:
            return len(self.calls)

    def call_names(self):
        with self.lock:
            return [call["name"] for call in self.calls]


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


@dataclass
class ExecutorHandle:
    proc: subprocess.Popen
    log_path: Path
    service: str


def stop_proc(handle: ExecutorHandle):
    proc = handle.proc
    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=8.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=5.0)


def start_executor(
    tmp_path,
    library,
    *,
    executable="sequence_executor",
    service_name="execute_behavior",
    robot_id="test-robot",
    params=None,
):
    base = {
        "action_library_path": str(library),
        "robot_id": robot_id,
        "lease_dir": str(tmp_path),
        "service_name": service_name,
        "step_timeout_sec": 2.0,
        "plan_timeout_sec": 15.0,
        "preflight_timeout_sec": 1.0,
        "observation_service": "/get_observation",
        "observation_timeout_sec": 1.0,
        "observation_settle_sec": 0.02,
        "max_steps": 8,
    }
    base.update(params or {})
    cmd = ["ros2", "run", "rosllm_executor", executable, "--ros-args"]
    for key, value in base.items():
        cmd.extend(["-p", f"{key}:={value}"])
    log_path = tmp_path / f"{service_name}-{uuid.uuid4().hex[:6]}.log"
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=os.environ.copy(),
    )
    probe = ClientHost(f"probe_{uuid.uuid4().hex[:8]}")
    try:
        probe.create_client(service_name, ExecuteBehavior)
        if not probe.wait_ready(service_name, 25.0):
            log.flush()
            raise AssertionError(
                f"executor did not become ready; log:\n{log_path.read_text()}"
            )
    finally:
        probe.shutdown()
    return ExecutorHandle(proc=proc, log_path=log_path, service=service_name)


@pytest.fixture()
def world():
    node = DummyWorld()
    host = NodeHost(node)
    try:
        yield node
    finally:
        host.stop()


@pytest.fixture()
def caller():
    hosts = []

    def factory():
        host = ClientHost(f"caller_{uuid.uuid4().hex[:8]}", num_threads=4)
        hosts.append(host)
        return host

    yield factory
    for host in hosts:
        host.shutdown()


def call_plan(host, handle, behavior, timeout=25.0):
    host.create_client(handle.service, ExecuteBehavior)
    result = host.call(
        handle.service,
        ExecuteBehavior.Request(behavior=behavior),
        timeout,
        request_id=f"test-{uuid.uuid4().hex[:6]}",
    )
    assert result.ok, result.error
    return result.response, parse_report(result.response.message)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_two_step_success_with_observation(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(tmp_path, library)
    try:
        host = caller()
        response, report = call_plan(
            host,
            handle,
            "\n".join(
                [
                    json.dumps({"name": "forward", "input": {"vel": 1.0}}),
                    json.dumps({"name": "turn", "input": {"vel": 0.5}}),
                ]
            ),
        )
        assert response.success and report.status is ExecStatus.OK
        assert report.completed_steps == 2 and len(report.steps) == 2
        assert [step.name for step in report.steps] == ["forward", "turn"]
        assert report.steps[0].observation["status"] == "fresh"
        assert report.steps[0].observation["payload"]["x"] == pytest.approx(1.0)
        assert world.call_names() == ["forward", "turn"]
        assert world.calls[1]["prev"] == world.calls[0]["output"]
    finally:
        stop_proc(handle)


def test_invalid_plans_are_rejected_before_execution(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(tmp_path, library)
    try:
        host = caller()
        cases = [
            ("", 1),
            ("{bad}", -7),
            ('["forward"]', -7),
            ('{"name": "evil"}', -7),
            ('{"name": "forward", "input": {"vel": 99}}', -7),
            ('{"name": "forward", "input": {"vel": NaN}}', -7),
            ('{"name": "forward", "extra": 1}', -7),
            ("\n".join(['{"name": "fail"}'] * 9), -12),
        ]
        for behavior, info in cases:
            response, report = call_plan(host, handle, behavior)
            assert not response.success
            assert report is not None and report.info == info, behavior
            assert world.call_count() == 0, behavior
    finally:
        stop_proc(handle)


def test_unavailable_service_rejected_before_any_action(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(tmp_path, library)
    try:
        host = caller()
        response, report = call_plan(
            host,
            handle,
            '{"name": "forward", "input": {"vel": 1}}\n{"name": "missing"}',
        )
        assert not response.success
        assert report.status is ExecStatus.UNAVAILABLE
        assert report.info == -2
        assert world.call_count() == 0
    finally:
        stop_proc(handle)


def test_explicit_failure_stops_plan(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(tmp_path, library)
    world.fail_on = {"turn"}
    try:
        host = caller()
        behavior = "\n".join(
            [
                '{"name": "forward", "input": {"vel": 1}}',
                '{"name": "turn", "input": {"vel": 1}}',
                '{"name": "forward", "input": {"vel": 1}}',
            ]
        )
        response, report = call_plan(host, handle, behavior)
        assert not response.success
        assert report.status is ExecStatus.FAILED and report.info == -6
        assert report.completed_steps == 1 and len(report.steps) == 2
        assert world.call_names() == ["forward", "turn"]
        assert "deliberate failure" in report.message
    finally:
        stop_proc(handle)


def test_timeout_is_reported_and_never_retried(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(
        tmp_path, library, params={"step_timeout_sec": 0.25, "plan_timeout_sec": 5.0}
    )
    world.delay = 0.8
    try:
        host = caller()
        response, report = call_plan(host, handle, '{"name": "forward", "input": {"vel": 1}}')
        assert not response.success
        assert report.status is ExecStatus.TIMEOUT and report.info == -9
        assert 0.2 <= report.elapsed_sec < 1.0
        assert world.call_count() == 1
        time.sleep(1.2)
        assert world.call_count() == 1  # late response and no retry
    finally:
        stop_proc(handle)


def test_timeout_blocks_new_plans_across_restart_until_confirmed(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    params = {"step_timeout_sec": 0.2, "enable_observation_snapshots": False}
    handle = start_executor(tmp_path, library, params=params)
    world.delay = 1.0
    behavior = '{"name":"forward","input":{"vel":1}}'
    try:
        host = caller()
        response, _ = call_plan(host, handle, behavior)
        assert response.info == -9
        response, _ = call_plan(host, handle, behavior)
        assert response.info == -10 and world.call_count() == 1
        stop_proc(handle)
        handle = start_executor(tmp_path, library, params=params)
        response, _ = call_plan(host, handle, behavior)
        assert response.info == -10 and world.call_count() == 1
        # The dummy action has actually finished before the operator acknowledgement.
        assert wait_until(lambda: bool(world.calls[0].get("output")))
        world.delay = 0.0
        reset = handle.service + "/confirm_stopped"
        host.create_client(reset, Trigger)
        ack = host.call(reset, Trigger.Request(), 3.0)
        assert ack.ok and ack.response.success
        response, _ = call_plan(host, handle, behavior)
        assert response.success and world.call_count() == 2
    finally:
        stop_proc(handle)


def test_concurrent_plans_only_one_is_accepted(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(tmp_path, library)
    world.delay = 1.0
    try:
        first_host, second_host = caller(), caller()
        result = {}

        def run_first():
            result["first"] = call_plan(
                first_host, handle, '{"name": "forward", "input": {"vel": 1}}'
            )

        thread = threading.Thread(target=run_first)
        thread.start()
        assert wait_until(lambda: world.call_count() >= 1)
        reset = handle.service + "/confirm_stopped"
        second_host.create_client(reset, Trigger)
        ack = second_host.call(reset, Trigger.Request(), 3.0)
        assert ack.ok and not ack.response.success
        response, report = call_plan(
            second_host, handle, '{"name": "forward", "input": {"vel": 1}}'
        )
        assert not response.success
        assert report.status is ExecStatus.BUSY and report.info == -8
        assert "another plan" in report.message
        thread.join(timeout=10.0)
        assert not thread.is_alive()
        assert result["first"][0].success

        response, report = call_plan(
            second_host, handle, '{"name": "forward", "input": {"vel": 1}}'
        )
        assert response.success and report.status is ExecStatus.OK
        assert world.call_count() == 2
    finally:
        stop_proc(handle)


def test_cross_process_lease_blocks_second_executor(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    first = start_executor(
        tmp_path, library, service_name="execute_behavior_a", robot_id="shared-robot"
    )
    second = start_executor(
        tmp_path, library, service_name="execute_behavior_b", robot_id="shared-robot"
    )
    world.delay = 1.0
    try:
        first_host, second_host = caller(), caller()
        result = {}

        def run_first():
            result["first"] = call_plan(
                first_host, first, '{"name": "forward", "input": {"vel": 1}}'
            )

        thread = threading.Thread(target=run_first)
        thread.start()
        assert wait_until(lambda: world.call_count() >= 1)
        response, report = call_plan(
            second_host, second, '{"name": "forward", "input": {"vel": 1}}'
        )
        assert not response.success and report.status is ExecStatus.BUSY
        thread.join(timeout=10.0)
        assert result["first"][0].success

        response, report = call_plan(
            second_host, second, '{"name": "forward", "input": {"vel": 1}}'
        )
        assert response.success and report.status is ExecStatus.OK
    finally:
        stop_proc(first)
        stop_proc(second)


@pytest.mark.parametrize("mode", ["stale", "missing", "error"])
def test_observation_snapshot_reports_staleness(caller, world, tmp_path, mode):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(tmp_path, library)
    world.obs_mode = mode
    try:
        host = caller()
        response, report = call_plan(
            host, handle, '{"name": "forward", "input": {"vel": 1}}'
        )
        assert response.success  # execution itself succeeded
        assert report.status is ExecStatus.OK
        assert report.steps[0].observation["status"] == mode
    finally:
        stop_proc(handle)


def test_bt_rejects_later_illegal_action_before_execution(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(
        tmp_path, library, executable="behavior_tree_executor"
    )
    try:
        host = caller()
        tree = """<Sequence>
                    <Action name="forward" input='{"vel": 1}'/>
                    <Action name="evil"/>
                  </Sequence>"""
        response, report = call_plan(host, handle, tree)
        assert not response.success and report.status is ExecStatus.INVALID
        assert world.call_count() == 0
    finally:
        stop_proc(handle)


def test_bt_retry_budget_rejected_before_execution(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(
        tmp_path, library, executable="behavior_tree_executor"
    )
    try:
        host = caller()
        tree = """<RetryUntilSuccessful num_attempts="10">
                    <Sequence>
                      <Action name="forward" input='{"vel": 1}'/>
                      <Action name="turn" input='{"vel": 1}'/>
                      <Action name="fail"/>
                      <Action name="forward" input='{"vel": 1}'/>
                    </Sequence>
                  </RetryUntilSuccessful>"""
        response, report = call_plan(host, handle, tree)
        assert not response.success and report.status is ExecStatus.LIMIT
        assert world.call_count() == 0
    finally:
        stop_proc(handle)


def test_bt_retries_explicit_failure_but_not_timeout(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(
        tmp_path, library, executable="behavior_tree_executor"
    )
    world.fail_on = {"fail"}
    try:
        host = caller()
        tree = """<RetryUntilSuccessful num_attempts="3">
                    <Action name="fail"/>
                  </RetryUntilSuccessful>"""
        response, report = call_plan(host, handle, tree)
        assert not response.success and report.status is ExecStatus.FAILED
        assert world.call_count() == 3
    finally:
        stop_proc(handle)

    timeout_handle = start_executor(
        tmp_path,
        library,
        executable="behavior_tree_executor",
        params={"step_timeout_sec": 0.2, "plan_timeout_sec": 5.0},
    )
    world.fail_on = set()
    world.delay = 0.8
    try:
        host = caller()
        tree = """<RetryUntilSuccessful num_attempts="5">
                    <Action name="slow" input='{"vel": 1}'/>
                  </RetryUntilSuccessful>"""
        response, report = call_plan(host, timeout_handle, tree)
        assert not response.success and report.status is ExecStatus.TIMEOUT
        assert world.call_count() == 4  # three failures + one slow attempt
        time.sleep(1.0)
        assert world.call_count() == 4
    finally:
        stop_proc(timeout_handle)


def test_bt_sequence_success_with_snapshots(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(
        tmp_path, library, executable="behavior_tree_executor"
    )
    try:
        host = caller()
        tree = """<Sequence>
                    <Action name="forward" input='{"vel": 1}'/>
                    <Action name="turn" input='{"vel": 0.5}'/>
                  </Sequence>"""
        response, report = call_plan(host, handle, tree)
        assert response.success and report.status is ExecStatus.OK
        assert [step.name for step in report.steps] == ["forward", "turn"]
        assert all(step.observation["status"] == "fresh" for step in report.steps)
    finally:
        stop_proc(handle)


def test_sigint_releases_lease(caller, world, tmp_path):
    library = make_library(tmp_path / "lib.yaml")
    handle = start_executor(tmp_path, library, service_name="execute_stop_test")
    host = caller()
    try:
        response, _ = call_plan(
            host, handle, '{"name": "forward", "input": {"vel": 1}}'
        )
        assert response.success
        os.killpg(os.getpgid(handle.proc.pid), signal.SIGINT)
        assert handle.proc.wait(timeout=10.0) == 0
    finally:
        stop_proc(handle)

    replacement = start_executor(
        tmp_path, library, service_name="execute_after_stop", robot_id="test-robot"
    )
    try:
        response, report = call_plan(
            caller(), replacement, '{"name": "forward", "input": {"vel": 1}}'
        )
        assert response.success and report.status is ExecStatus.OK
    finally:
        stop_proc(replacement)
