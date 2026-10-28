"""Real LLM node for OpenAI-compatible chat-completion endpoints.

The configuration (model, base URL, temperature, timeout, key environment
variable) comes from ROS parameters.  The secret is read from the process
environment or from a private ``env_file`` (default
``~/.config/rosllm/llm.env``) at startup; it is never accepted as a ROS
parameter, never logged and never echoed in responses.
"""

from __future__ import annotations

import signal
import os
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_msgs.msg import LLMResponse
from rosllm_srvs.srv import LLM
from std_msgs.msg import String

from rosllm_core.llm import LLMConfig, chat_completion, read_env_value, resolve_api_key


class LLMNode(Node):
    def __init__(self):
        super().__init__("llm_node")
        self.declare_parameter("model", "")
        self.declare_parameter("base_url", "http://127.0.0.1:8000/v1")
        self.declare_parameter("temperature", 0.8)
        self.declare_parameter("timeout_sec", 30.0)
        self.declare_parameter("api_key_env", "ROSLLM_API_KEY")
        self.declare_parameter("env_file", "~/.config/rosllm/llm.env")
        self.declare_parameter("max_response_bytes", 262144)
        self.declare_parameter("num_workers", 4)
        self.declare_parameter("log_prompts", False)
        self._counter = 0
        self._counter_lock = threading.Lock()
        self._log_prompts = bool(self.get_parameter("log_prompts").value)
        workers = int(self.get_parameter("num_workers").value)
        self._pool = ThreadPoolExecutor(max_workers=max(1, min(8, workers)))
        self._group = ReentrantCallbackGroup()
        self.create_service(LLM, "call_llm", self._handle, callback_group=self._group)
        self.create_subscription(String, "llm_prompt", self._on_prompt, 10)
        self.publisher = self.create_publisher(LLMResponse, "llm_response", 10)

        self._config = LLMConfig(
            model=str(self.get_parameter("model").value),
            base_url=str(self.get_parameter("base_url").value),
            temperature=float(self.get_parameter("temperature").value),
            timeout_sec=float(self.get_parameter("timeout_sec").value),
            api_key_env=str(self.get_parameter("api_key_env").value),
            max_response_bytes=int(self.get_parameter("max_response_bytes").value),
        )
        self._api_key = ""
        self._api_key_source = "none"
        try:
            self._config.validated()
            self._resolve_api_key()
            self.get_logger().info(
                f"llm_node ready: model={self._config.model!r} "
                f"endpoint={self._config.endpoint!r} timeout={self._config.timeout_sec}s "
                f"api_key_source={self._api_key_source}"
            )
        except (TypeError, ValueError) as exc:
            self.get_logger().error(f"invalid LLM configuration: {exc}")

    def _resolve_api_key(self) -> None:
        key, _ = resolve_api_key(self._config, os.environ)
        if key:
            self._api_key = key
            self._api_key_source = "environment"
            return
        env_file = str(self.get_parameter("env_file").value).strip()
        if not env_file:
            return
        key, error = read_env_value(env_file, self._config.api_key_env)
        if key:
            self._api_key = key
            self._api_key_source = "env file"
            try:
                mode = Path(env_file).expanduser().stat().st_mode
                if mode & 0o077:
                    self.get_logger().warning(
                        "env_file is readable by group/others; chmod 600 is recommended"
                    )
            except OSError:
                pass
        elif self._config.is_loopback:
            self.get_logger().info(f"no API key found ({error}); loopback endpoint may allow this")
        else:
            self.get_logger().error(
                f"no API key in environment or env_file: {error}; "
                "requests will fail with auth_missing"
            )

    def _next_id(self) -> int:
        with self._counter_lock:
            self._counter += 1
            return self._counter

    def _complete(self, prompt: str, request_id: int, source: str) -> LLMResponse:
        start = time.time_ns()
        if self._log_prompts:
            self.get_logger().info(f"request {request_id} prompt: {prompt}")
        result = chat_completion(prompt, self._config, api_key=self._api_key or None)
        end = time.time_ns()
        message = LLMResponse()
        message.header.stamp = self.get_clock().now().to_msg()
        message.req = request_id
        message.success = result.ok
        message.response = result.text
        message.req_time = start
        message.res_time = end
        if result.ok:
            message.info = (
                f"successfully received response from '{self._config.model}'"
            )
            self.get_logger().info(
                f"request {request_id} ({source}): ok in {result.elapsed_sec:.2f}s"
            )
        else:
            message.info = f"LLM error [{result.error_code}]: {result.error}"
            self.get_logger().warning(
                f"request {request_id} ({source}): {result.error_code} in "
                f"{result.elapsed_sec:.2f}s: {result.error}"
            )
        return message

    def _handle(self, request, response):
        request_id = self._next_id()
        self.get_logger().info(
            f"request {request_id}: received {len(request.prompt)} prompt bytes"
        )
        response.response = self._complete(request.prompt, request_id, "service")
        return response

    def _on_prompt(self, message: String) -> None:
        request_id = self._next_id()

        def work():
            try:
                response = self._complete(message.data, request_id, "topic")
                self.publisher.publish(response)
            except Exception as exc:  # noqa: BLE001 - keep the node alive
                self.get_logger().error(
                    f"request {request_id}: unhandled LLM error: "
                    f"{type(exc).__name__}: {exc}"
                )

        self._pool.submit(work)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)


def main() -> None:
    rclpy.init()
    node = None
    executor = None
    try:
        node = LLMNode()
        executor = MultiThreadedExecutor(num_threads=6)
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
                node.shutdown()
            except BaseException:
                pass
            try:
                node.destroy_node()
            except BaseException:
                pass
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
