"""Offline fake LLM: returns the contents of a configured file."""

from __future__ import annotations

import signal
import threading
import time
import traceback
from pathlib import Path

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_msgs.msg import LLMResponse
from rosllm_srvs.srv import LLM
from std_msgs.msg import String

MAX_RESPONSE_BYTES = 262144


class FakeLLMNode(Node):
    def __init__(self):
        super().__init__("fake_llm_node")
        self.declare_parameter("path", "")
        self.declare_parameter("delay_sec", 0.0)
        self.path = Path(str(self.get_parameter("path").value)).expanduser()
        self.delay = max(0.0, min(30.0, float(self.get_parameter("delay_sec").value)))
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._group = ReentrantCallbackGroup()
        self.create_service(LLM, "call_llm", self._handle, callback_group=self._group)
        self.create_subscription(String, "llm_prompt", self._on_prompt, 10)
        self.publisher = self.create_publisher(LLMResponse, "llm_response", 10)
        if not str(self.path) or not self.path.is_file():
            self.get_logger().error(
                f"fake LLM response file is missing: {self.path}; "
                "requests will fail until it is configured"
            )
        else:
            self.get_logger().info(f"fake LLM will read responses from {self.path}")

    def _next_id(self) -> int:
        with self._counter_lock:
            self._counter += 1
            return self._counter

    def _build_response(self, request_id: int) -> LLMResponse:
        start = time.time_ns()
        if self.delay > 0:
            time.sleep(self.delay)
        try:
            if not self.path.is_file():
                raise FileNotFoundError(f"response file not found: {self.path}")
            raw = self.path.read_text(encoding="utf-8")
            if len(raw.encode("utf-8")) > MAX_RESPONSE_BYTES:
                raise ValueError(f"response file exceeds {MAX_RESPONSE_BYTES} bytes")
            text = raw
            success = True
            info = "successfully received response from fake LLM"
        except Exception:
            text = ""
            success = False
            info = traceback.format_exc()[:2048]
        end = time.time_ns()
        message = LLMResponse()
        message.header.stamp = self.get_clock().now().to_msg()
        message.req = request_id
        message.success = success
        message.info = info
        message.response = text
        message.req_time = start
        message.res_time = end
        return message

    def _handle(self, request, response):
        request_id = self._next_id()
        self.get_logger().info(
            f"fake LLM request {request_id}: {len(request.prompt)} prompt bytes"
        )
        response.response = self._build_response(request_id)
        return response

    def _on_prompt(self, message: String) -> None:
        request_id = self._next_id()
        self.publisher.publish(self._build_response(request_id))


def main() -> None:
    rclpy.init()
    node = None
    executor = None
    try:
        node = FakeLLMNode()
        executor = MultiThreadedExecutor(num_threads=2)
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
            try:
                node.destroy_node()
            except BaseException:
                pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
