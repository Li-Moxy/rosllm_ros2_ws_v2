"""Regression: a live adapter must not hide an upstream pose outage."""
import os
import random
import threading
import time

import rclpy
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_srvs.srv import Observation
from turtlesim.msg import Pose

from rosllm_core.action_client import ClientHost
from rosllm_core.observation import ObservationView
from rosllm_observation.observation_manager_node import ObservationManagerNode
from rosllm_turtlesim.turtle_observation import TurtleObservation


def test_pose_outage_becomes_stale_and_new_pose_recovers(tmp_path):
    os.environ["ROS_DOMAIN_ID"] = str(random.randint(10, 220))
    os.environ["ROS_LOCALHOST_ONLY"] = "1"
    config = tmp_path / "observations.yaml"
    config.write_text("staleness_sec: 0.3\nobservations_topics:\n"
                      "  - name: turtle_pose\n    topic: /turtle1/observation\n")
    rclpy.init(args=["--ros-args", "-p", f"config_filename:={config}"])
    adapter = TurtleObservation()
    manager = ObservationManagerNode()
    sensor = Node("one_shot_pose_source")
    publisher = sensor.create_publisher(Pose, "/turtle1/pose", 10)
    executor = MultiThreadedExecutor(num_threads=4)
    for node in (adapter, manager, sensor):
        executor.add_node(node)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    caller = ClientHost("pose_source_regression")
    caller.create_client("/get_observation", Observation)

    def wait_entry(predicate, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = caller.call("/get_observation", Observation.Request(), 1.0)
            if result.ok:
                entry = ObservationView.from_messages(result.response.observation).get("turtle_pose")
                if entry and predicate(entry):
                    return entry
            time.sleep(0.02)
        raise AssertionError("expected observation state was never received")

    try:
        assert caller.wait_ready("/get_observation", 4.0)
        deadline = time.monotonic() + 4.0
        while publisher.get_subscription_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert publisher.get_subscription_count() > 0
        publisher.publish(Pose(x=5.0, y=5.0))
        assert wait_entry(lambda entry: entry.status == "fresh").payload["position"] == {"x": 5.0, "y": 5.0}
        stale = wait_entry(lambda entry: entry.status == "stale")
        assert stale.age_sec >= 0.3
        publisher.publish(Pose(x=5.0, y=6.0))
        recovered = wait_entry(lambda entry: entry.status == "fresh" and entry.payload["y"] == 6.0)
        assert recovered.payload["position"]["y"] == 6.0
    finally:
        executor.shutdown(timeout_sec=3.0)
        thread.join(timeout=3.0)
        caller.shutdown()
        manager.shutdown()
        for node in (adapter, manager, sensor):
            node.destroy_node()
        rclpy.shutdown()
