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
import yaml
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_msgs.msg import Observation as ObservationMsg
from rosllm_srvs.srv import Observation
from std_msgs.msg import String

from rosllm_core.action_client import ClientHost
from rosllm_core.observation import ObservationView
from rosllm_core.jsonutil import dumps

os.environ["ROS_DOMAIN_ID"] = str(random.randint(10, 220))
os.environ["ROS_LOCALHOST_ONLY"] = "1"


@pytest.fixture(scope="module", autouse=True)
def ros_context():
    rclpy.init()
    yield
    rclpy.shutdown()


class Publisher(Node):
    def __init__(self, prefix, value=1.0):
        super().__init__(f"obs_publisher_{uuid.uuid4().hex[:8]}")
        self.value = value
        self.publisher = self.create_publisher(String, f"/{prefix}/obs", 10)
        self.timer = self.create_timer(0.1, self._publish)

    def _publish(self):
        message = String()
        message.data = dumps({"value": self.value})
        self.publisher.publish(message)


class Sensor(Node):
    def __init__(self, prefix):
        super().__init__(f"obs_sensor_{uuid.uuid4().hex[:8]}")
        self.create_service(
            Observation,
            f"/{prefix}/sensor",
            self._handle,
            callback_group=ReentrantCallbackGroup(),
        )

    def _handle(self, request, response):
        message = ObservationMsg()
        message.name = "sensor"
        message.text = [dumps({"mode": "A"})]
        response.observation = [message]
        return response


class NodeHost:
    def __init__(self, node):
        self.node = node
        self.executor = MultiThreadedExecutor(num_threads=3)
        self.executor.add_node(node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    def stop(self):
        self.executor.shutdown(timeout_sec=2.0)
        self.thread.join(timeout=2.0)
        self.node.destroy_node()

    def wait_subscribers(self, publisher, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if publisher.get_subscription_count() > 0:
                return
            time.sleep(0.02)


def write_config(path: Path, staleness: float, topics, services):
    path.write_text(
        yaml.safe_dump(
            {
                "staleness_sec": staleness,
                "observations_topics": topics,
                "observations_srvs": services,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


class ManagerProc:
    def __init__(self, config, service):
        self.service = service
        cmd = [
            "ros2",
            "run",
            "rosllm_observation",
            "observation_manager",
            "--ros-args",
            "-p",
            f"config_filename:={config}",
            "-p",
            f"service_name:={service}",
            "-p",
            "total_timeout_sec:=1.0",
        ]
        self.log_path = Path(f"/tmp/rosllm_obs_{uuid.uuid4().hex[:8]}.log")
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            cmd,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=os.environ.copy(),
        )

    def wait_ready(self, timeout=25.0):
        probe = ClientHost(f"obs_probe_{uuid.uuid4().hex[:8]}")
        try:
            probe.create_client(self.service, Observation)
            ready = probe.wait_ready(self.service, timeout)
        finally:
            probe.shutdown()
        self.log.flush()
        if not ready or self.proc.poll() is not None:
            raise AssertionError(f"manager failed:\n{self.log_path.read_text()}")
        return self

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


def call_manager(service):
    host = ClientHost(f"obs_client_{uuid.uuid4().hex[:8]}")
    try:
        host.create_client(service, Observation)
        result = host.call(service, Observation.Request(), 5.0, request_id="obs")
        assert result.ok, result.error
        return ObservationView.from_messages(result.response.observation), result
    finally:
        host.shutdown()


def test_fresh_topic_observation(tmp_path):
    prefix = f"t{uuid.uuid4().hex[:10]}"
    service = f"/{prefix}/get_observation"
    config = write_config(
        tmp_path / "obs.yaml",
        2.0,
        [{"name": "pose", "topic": f"/{prefix}/obs"}],
        [],
    )
    publisher = Publisher(prefix, 3.5)
    host = NodeHost(publisher)
    manager = ManagerProc(config, service)
    try:
        manager.wait_ready()
        host.wait_subscribers(publisher.publisher)
        deadline = time.monotonic() + 5.0
        view = None
        while time.monotonic() < deadline:
            view, _ = call_manager(service)
            if view.get("pose") is not None and view.get("pose").payload:
                break
            time.sleep(0.1)
        entry = view.get("pose")
        assert entry is not None
        assert entry.status == "fresh"
        assert entry.payload == {"value": 3.5}
        assert entry.age_sec is not None and entry.age_sec < 1.0
        assert view.is_fresh("pose", 1.0)[0]
    finally:
        manager.stop()
        host.stop()


def test_stale_and_missing_observations(tmp_path):
    prefix = f"t{uuid.uuid4().hex[:10]}"
    service = f"/{prefix}/get_observation"
    config = write_config(
        tmp_path / "obs.yaml",
        0.3,
        [
            {"name": "stale", "topic": f"/{prefix}/stale"},
            {"name": "never", "topic": f"/{prefix}/never"},
        ],
        [],
    )
    stale_pub = Node(f"stale_pub_{uuid.uuid4().hex[:8]}")
    publisher = stale_pub.create_publisher(String, f"/{prefix}/stale", 10)
    stale_host = NodeHost(stale_pub)
    manager = ManagerProc(config, service)
    try:
        manager.wait_ready()
        deadline = time.monotonic() + 5.0
        while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        message = String()
        message.data = dumps({"value": 1})
        publisher.publish(message)
        time.sleep(0.7)
        view, _ = call_manager(service)
        assert view.status("stale") == "stale"
        assert view.status("never") == "missing"
        assert not view.is_fresh("stale", 0.3)[0]
        assert not view.is_fresh("never", 0.3)[0]
        assert view.get("stale").payload == {"value": 1}
    finally:
        manager.stop()
        stale_host.stop()


def test_service_observation_and_unavailable_service(tmp_path):
    prefix = f"t{uuid.uuid4().hex[:10]}"
    service = f"/{prefix}/get_observation"
    sensor = Sensor(prefix)
    sensor_host = NodeHost(sensor)
    config = write_config(
        tmp_path / "obs.yaml",
        2.0,
        [],
        [
            {"name": "sensor", "service": f"/{prefix}/sensor", "timeout_sec": 0.5},
            {"name": "dead", "service": f"/{prefix}/does_not_exist", "timeout_sec": 0.2},
        ],
    )
    manager = ManagerProc(config, service)
    try:
        manager.wait_ready()
        view, _ = call_manager(service)
        assert view.status("sensor") == "fresh"
        assert view.get("sensor").payload == {"mode": "A"}
        assert view.status("dead") in ("missing", "error")
        assert not view.get("dead").payload
        # The manager remains alive after a failed sub-service call.
        view2, _ = call_manager(service)
        assert view2.status("sensor") == "fresh"
    finally:
        manager.stop()
        sensor_host.stop()
