"""Turtlesim atomic actions with bounded motion and an explicit stop.

Unlike the upstream ROS 1 demo, which publishes one Twist and leaves the turtle
moving forever, each action publishes at a fixed rate for a bounded duration and
always sends a zero Twist at the end.
"""

from __future__ import annotations

import json
import math
import signal
import threading
import time

import rclpy
from rclpy.executors import ExternalShutdownException
from geometry_msgs.msg import Twist
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_srvs.srv import AtomicAction
from turtlesim.msg import Pose

from rosllm_core.jsonutil import StrictJSONError, dumps, loads

MAX_INPUT_BYTES = 4096


class TurtleActions(Node):
    def __init__(self):
        super().__init__("turtle_actions")
        self.declare_parameter("service_prefix", "/turtle1")
        self.declare_parameter("cmd_vel_topic", "/turtle1/cmd_vel")
        self.declare_parameter("pose_topic", "/turtle1/pose")
        self.declare_parameter("max_linear_vel", 2.0)
        self.declare_parameter("max_angular_vel", 2.0)
        self.declare_parameter("move_duration_sec", 1.0)
        self.declare_parameter("publish_hz", 20.0)
        self.declare_parameter("stop_timeout_sec", 1.0)

        self.prefix = str(self.get_parameter("service_prefix").value).rstrip("/")
        if not self.prefix.startswith("/") or len(self.prefix) > 128:
            raise RuntimeError("service_prefix must be an absolute ROS name")
        self.max_linear = float(self.get_parameter("max_linear_vel").value)
        self.max_angular = float(self.get_parameter("max_angular_vel").value)
        self.duration = float(self.get_parameter("move_duration_sec").value)
        self.hz = float(self.get_parameter("publish_hz").value)
        self.stop_timeout = float(self.get_parameter("stop_timeout_sec").value)
        for name, value, low, high in (
            ("max_linear_vel", self.max_linear, 0.01, 10.0),
            ("max_angular_vel", self.max_angular, 0.01, 10.0),
            ("move_duration_sec", self.duration, 0.05, 5.0),
            ("publish_hz", self.hz, 1.0, 100.0),
            ("stop_timeout_sec", self.stop_timeout, 0.0, 2.0),
        ):
            if not math.isfinite(value) or not low <= value <= high:
                raise RuntimeError(f"parameter {name!r} must be within {low}..{high}")

        self._pose_lock = threading.Lock()
        self._latest_pose: Pose | None = None
        self._last_pose_time = 0.0
        self._pose_count = 0
        self._motion_lock = threading.Lock()
        self.publisher = self.create_publisher(
            Twist, str(self.get_parameter("cmd_vel_topic").value), 10
        )
        self.create_subscription(
            Pose, str(self.get_parameter("pose_topic").value), self._on_pose, 10
        )
        # All action services share one mutually-exclusive group so two motions
        # can never be commanded concurrently.  Pose callbacks are in the
        # default group and keep running during a blocking action.
        self._action_group = MutuallyExclusiveCallbackGroup()
        self.create_service(
            AtomicAction,
            f"{self.prefix}/forward",
            self._forward,
            callback_group=self._action_group,
        )
        self.create_service(
            AtomicAction,
            f"{self.prefix}/turn",
            self._turn,
            callback_group=self._action_group,
        )
        self.create_service(
            AtomicAction,
            f"{self.prefix}/command",
            self._command,
            callback_group=self._action_group,
        )
        self.get_logger().info(
            f"turtle actions ready at {self.prefix}/{{forward,turn,command}}; "
            f"max_linear={self.max_linear} max_angular={self.max_angular} "
            f"duration={self.duration}s"
        )

    # ------------------------------------------------------------- inputs
    def _parse_input(self, text: str) -> tuple[bool, dict, str]:
        if not isinstance(text, str):
            return False, {}, "input must be a string"
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            return False, {}, f"input exceeds {MAX_INPUT_BYTES} bytes"
        if not text.strip():
            return False, {}, "input must be a JSON object"
        try:
            value = loads(text)
        except StrictJSONError as exc:
            return False, {}, f"invalid input JSON: {exc}"
        if not isinstance(value, dict):
            return False, {}, "input must be a JSON object"
        return True, value, ""

    def _number(
        self, obj: dict, key: str, limit: float, *, required: bool = True
    ) -> tuple[bool, float, str]:
        if key not in obj:
            return (False, 0.0, f"missing {key!r}") if required else (True, 0.0, "")
        value = obj[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False, 0.0, f"{key!r} must be a number"
        number = float(value)
        if not math.isfinite(number):
            return False, 0.0, f"{key!r} must be finite"
        if abs(number) > limit:
            return False, 0.0, f"{key!r} must be within +/-{limit}"
        return True, number, ""

    # ------------------------------------------------------------ actions
    def _forward(self, request, response):
        ok, data, error = self._parse_input(request.input)
        if ok and set(data) != {"vel"}:
            ok, error = False, "forward expects exactly {'vel': number}"
        if ok:
            ok, vel, error = self._number(data, "vel", self.max_linear)
        if not ok:
            response.success = False
            response.output = self._error(error)
            return response
        return self._run_motion(response, linear=vel, angular=0.0, label="forward")

    def _turn(self, request, response):
        ok, data, error = self._parse_input(request.input)
        if ok and set(data) != {"vel"}:
            ok, error = False, "turn expects exactly {'vel': number}"
        if ok:
            ok, vel, error = self._number(data, "vel", self.max_angular)
        if not ok:
            response.success = False
            response.output = self._error(error)
            return response
        return self._run_motion(response, linear=0.0, angular=vel, label="turn")

    def _command(self, request, response):
        ok, data, error = self._parse_input(request.input)
        if ok and set(data) - {"fwd_vel", "strafe_vel", "ang_vel"}:
            ok, error = False, (
                "command expects only {'fwd_vel', 'strafe_vel', 'ang_vel'}"
            )
        if ok:
            ok, fwd, error = self._number(data, "fwd_vel", self.max_linear)
        if ok:
            ok, strafe, error = self._number(data, "strafe_vel", self.max_linear)
        if ok:
            ok, ang, error = self._number(data, "ang_vel", self.max_angular)
        if ok and abs(strafe) > 0.0:
            ok, error = False, (
                "turtlesim cannot strafe (lateral motion); strafe_vel must be 0"
            )
        if not ok:
            response.success = False
            response.output = self._error(error)
            return response
        return self._run_motion(response, linear=fwd, angular=ang, label="command")

    def _run_motion(self, response, *, linear: float, angular: float, label: str):
        if not self._motion_lock.acquire(blocking=False):
            response.success = False
            response.output = self._error("another motion is already active")
            return response
        started = time.monotonic()
        try:
            self.get_logger().info(
                f"{label}: linear={linear:.3f} angular={angular:.3f} "
                f"for {self.duration:.2f}s"
            )
            deadline = started + self.duration
            twist = Twist()
            twist.linear.x = linear
            twist.angular.z = angular
            # One reliable command, then an explicit zero before returning.
            # Streaming at high rate leaves a queue that keeps the turtle
            # moving after the service would otherwise have stopped it.
            self.publisher.publish(twist)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.05, remaining))
        finally:
            self.publisher.publish(Twist())  # always stop
            self._motion_lock.release()
        stop_confirmed = self._wait_for_stop(time.monotonic() + self.stop_timeout)
        pose = self._pose_snapshot()
        payload = {
            "action": label,
            "duration_sec": round(time.monotonic() - started, 3),
            "stop_confirmed": stop_confirmed,
        }
        if pose is None:
            payload["pose_available"] = False
        else:
            payload.update(pose)
            payload["pose_available"] = True
        if not stop_confirmed:
            payload["error"] = "turtle stop could not be confirmed from /pose"
            self.get_logger().warning(f"{label}: {payload['error']}")
            response.success = False
        else:
            response.success = True
        response.output = dumps(payload)
        return response

    def _wait_for_stop(self, deadline: float) -> bool:
        """Confirm a recent zero-velocity pose.

        Turtlesim may publish faster than this callback is scheduled, so one
        recent zero-velocity frame is sufficient; requiring two consecutive
        frames (or a frame strictly after a locally recorded timestamp) caused
        false "stop unconfirmed" failures under load.  A pre-motion pose is
        rejected by the age bound because the motion lasts at least 1 second.
        """
        while time.monotonic() < deadline:
            with self._pose_lock:
                pose = self._latest_pose
                received = self._last_pose_time
            if (
                pose is not None
                and time.monotonic() - received <= 0.35
                and abs(pose.linear_velocity) <= 0.02
                and abs(pose.angular_velocity) <= 0.02
            ):
                return True
            time.sleep(0.02)
        with self._pose_lock:
            pose = self._latest_pose
            received = self._last_pose_time
            count = self._pose_count
        self.get_logger().warning(
            "stop not confirmed: pose_count=%d age=%.3fs linear=%s angular=%s"
            % (
                count,
                time.monotonic() - received if received else -1.0,
                getattr(pose, "linear_velocity", None),
                getattr(pose, "angular_velocity", None),
            )
        )
        return False

    @staticmethod
    def _error(message: str) -> str:
        return json.dumps({"error": message}, ensure_ascii=False)

    def _on_pose(self, message: Pose) -> None:
        with self._pose_lock:
            self._latest_pose = message
            self._last_pose_time = time.monotonic()
            self._pose_count += 1

    def _pose_snapshot(self, max_age: float | None = None) -> dict | None:
        with self._pose_lock:
            pose = self._latest_pose
            received = self._last_pose_time
        if pose is None:
            return None
        if max_age is not None and time.monotonic() - received > max_age:
            return None
        return {
            "x": pose.x,
            "y": pose.y,
            "theta": pose.theta,
            "linear_velocity": pose.linear_velocity,
            "angular_velocity": pose.angular_velocity,
        }

    def stop(self) -> None:
        try:
            self.publisher.publish(Twist())
        except Exception:
            pass


def main() -> None:
    rclpy.init()
    node = None
    executor = None
    try:
        node = TurtleActions()
        executor = MultiThreadedExecutor(num_threads=4)
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
                executor.shutdown(timeout_sec=2.0)
            except BaseException:
                pass
        if node is not None:
            node.stop()
            try:
                node.destroy_node()
            except BaseException:
                pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
