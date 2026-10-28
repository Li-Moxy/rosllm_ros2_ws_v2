"""Publish the current turtlesim pose as a JSON String observation."""

from __future__ import annotations

import json
import math
import signal
import threading

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import String
from turtlesim.msg import Pose


class TurtleObservation(Node):
    def __init__(self):
        super().__init__("turtle_observation")
        self.declare_parameter("pose_topic", "/turtle1/pose")
        self.declare_parameter("observation_topic", "/turtle1/observation")
        self.declare_parameter("rate_hz", 10.0)
        self.rate_hz = float(self.get_parameter("rate_hz").value)
        if not math.isfinite(self.rate_hz) or not 0.1 <= self.rate_hz <= 100.0:
            raise RuntimeError("parameter 'rate_hz' must be within 0.1..100")
        self._lock = threading.Lock()
        self._pose: Pose | None = None
        self._pose_updated = False
        self.create_subscription(
            Pose, str(self.get_parameter("pose_topic").value), self._on_pose, 10
        )
        self.publisher = self.create_publisher(
            String, str(self.get_parameter("observation_topic").value), 10
        )
        self.create_timer(1.0 / self.rate_hz, self._publish)
        self.get_logger().info(
            f"turtle observation ready at {self.get_parameter('observation_topic').value} "
            f"({self.rate_hz} Hz)"
        )

    def _on_pose(self, message: Pose) -> None:
        with self._lock:
            self._pose = message
            self._pose_updated = True

    def _publish(self) -> None:
        with self._lock:
            # Publish only newly received source data. Repeating a cached pose
            # would reset the observation manager's age during a sensor outage.
            if not self._pose_updated:
                return
            pose = self._pose
            self._pose_updated = False
        if pose is None:
            return
        message = String()
        message.data = json.dumps(
            {
                "x": pose.x,
                "y": pose.y,
                "position": {"x": pose.x, "y": pose.y},
                "theta": pose.theta,
                "linear_velocity": pose.linear_velocity,
                "angular_velocity": pose.angular_velocity,
            },
            separators=(",", ":"),
            allow_nan=False,
        )
        self.publisher.publish(message)


def main() -> None:
    rclpy.init()
    node = None
    try:
        node = TurtleObservation()
        rclpy.spin(node)
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
        if node is not None:
            try:
                node.destroy_node()
            except BaseException:
                pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
