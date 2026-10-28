"""Asynchronous ROS 2 service calls with explicit outcomes.

The caller never spins the node it lives in.  A dedicated client node is spun
by its own executor thread, so blocking plan code cannot deadlock the service
server that invoked it, and completed responses are not mistaken for timeouts.

A local timeout is *not* a cancellation of the remote action.  ``future.cancel``
only stops this process from waiting; the server may still execute the request.
Callers must never automatically resend an action after ``TIMEOUT``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import rclpy
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor


class CallStatus(str, Enum):
    OK = "ok"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class CallResult:
    status: CallStatus
    response: Any = None
    error: str = ""
    request_id: str = ""
    elapsed_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status is CallStatus.OK


class ClientHost:
    """Owns one ROS 2 node and a spinning executor thread for client calls."""

    def __init__(self, node_name: str, *, num_threads: int = 3):
        self.node = rclpy.create_node(node_name, use_global_arguments=False)
        self._group = ReentrantCallbackGroup()
        self._executor = MultiThreadedExecutor(num_threads=max(2, num_threads))
        self._executor.add_node(self.node)
        self._lock = threading.Lock()
        self._clients: dict[str, Any] = {}
        self._client_locks: dict[str, threading.Lock] = {}
        self._closed = False
        self._counter = 0
        self._thread = threading.Thread(
            target=self._spin, name=f"{node_name}-executor", daemon=True
        )
        self._thread.start()

    def _spin(self) -> None:
        try:
            self._executor.spin()
        except Exception as exc:  # pragma: no cover - executor teardown race
            if not self._closed:
                self.node.get_logger().error(f"client executor stopped: {exc}")

    @property
    def logger(self):
        return self.node.get_logger()

    def create_client(self, service: str, srv_type: Any):
        with self._lock:
            existing = self._clients.get(service)
            if existing is not None:
                if existing.srv_type is not srv_type:
                    raise ValueError(
                        f"service {service!r} already registered with a different type"
                    )
                return existing
            client = self.node.create_client(
                srv_type, service, callback_group=self._group
            )
            self._clients[service] = client
            self._client_locks[service] = threading.Lock()
            return client

    def has_client(self, service: str) -> bool:
        return service in self._clients

    def wait_ready(self, service: str, timeout_sec: float) -> bool:
        client = self._clients.get(service)
        if client is None:
            return False
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while True:
            try:
                if client.service_is_ready():
                    return True
            except Exception:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)

    def call(
        self,
        service: str,
        request: Any,
        timeout_sec: float,
        *,
        request_id: str = "",
        ready_timeout_sec: float | None = None,
    ) -> CallResult:
        started = time.monotonic()
        with self._lock:
            self._counter += 1
            counter = self._counter
        if not request_id:
            request_id = f"{service}#{counter}"
        client = self._clients.get(service)
        if client is None:
            return CallResult(
                CallStatus.ERROR,
                error=f"no client registered for service {service!r}",
                request_id=request_id,
            )
        if timeout_sec <= 0:
            return CallResult(
                CallStatus.TIMEOUT,
                error="call timeout budget is exhausted",
                request_id=request_id,
            )

        client_lock = self._client_locks[service]
        if not client_lock.acquire(timeout=timeout_sec):
            return CallResult(
                CallStatus.ERROR,
                error=f"client for {service!r} is busy",
                request_id=request_id,
                elapsed_sec=time.monotonic() - started,
            )
        try:
            remaining = max(0.0, timeout_sec - (time.monotonic() - started))
            ready_timeout = (
                min(5.0, remaining) if ready_timeout_sec is None else ready_timeout_sec
            )
            if not client.service_is_ready() and not self.wait_ready(
                service, ready_timeout
            ):
                return CallResult(
                    CallStatus.UNAVAILABLE,
                    error=f"service {service!r} is unavailable",
                    request_id=request_id,
                    elapsed_sec=time.monotonic() - started,
                )
            remaining = max(0.0, timeout_sec - (time.monotonic() - started))
            if remaining <= 0:
                return CallResult(
                    CallStatus.TIMEOUT,
                    error="call timeout budget is exhausted",
                    request_id=request_id,
                    elapsed_sec=time.monotonic() - started,
                )
            try:
                future = client.call_async(request)
            except Exception as exc:
                return CallResult(
                    CallStatus.ERROR,
                    error=f"failed to send request: {type(exc).__name__}: {exc}",
                    request_id=request_id,
                    elapsed_sec=time.monotonic() - started,
                )

            done = threading.Event()
            abandoned = threading.Event()
            state: dict[str, Any] = {}

            def on_done(fut, *, _state=state, _done=done, _abandoned=abandoned):
                _state["finished_at"] = time.monotonic()
                try:
                    if fut.cancelled():
                        _state["cancelled"] = True
                    else:
                        _state["response"] = fut.result()
                except Exception as exc:  # noqa: BLE001 - propagated as a value
                    _state["error"] = exc
                if _abandoned.is_set():
                    self.node.get_logger().warning(
                        f"late response for abandoned request {request_id} ignored"
                    )
                _done.set()

            future.add_done_callback(on_done)
            if done.wait(remaining):
                return self._result_from_state(state, request_id, started)

            # Boundary race: the response may have completed between wait() and cancel().
            if done.is_set():
                return self._result_from_state(state, request_id, started)
            abandoned.set()
            try:
                future.cancel()
            except Exception:
                pass
            if done.is_set() and "response" in state and not state.get("cancelled"):
                return self._result_from_state(state, request_id, started)
            return CallResult(
                CallStatus.TIMEOUT,
                error=(
                    f"no response from {service!r} within {timeout_sec:.2f}s; "
                    "the remote action may still be running"
                ),
                request_id=request_id,
                elapsed_sec=time.monotonic() - started,
            )
        finally:
            client_lock.release()

    def _result_from_state(
        self,
        state: dict[str, Any],
        request_id: str,
        started: float,
    ) -> CallResult:
        elapsed = time.monotonic() - started
        if state.get("cancelled"):
            return CallResult(
                CallStatus.CANCELLED,
                error="request was cancelled locally",
                request_id=request_id,
                elapsed_sec=elapsed,
            )
        if "error" in state:
            exc = state["error"]
            return CallResult(
                CallStatus.ERROR,
                error=f"service call failed: {type(exc).__name__}: {exc}",
                request_id=request_id,
                elapsed_sec=elapsed,
            )
        response = state.get("response")
        if response is None:
            return CallResult(
                CallStatus.ERROR,
                error="service returned no response",
                request_id=request_id,
                elapsed_sec=elapsed,
            )
        return CallResult(
            CallStatus.OK,
            response=response,
            request_id=request_id,
            elapsed_sec=elapsed,
        )

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._executor.shutdown(timeout_sec=2.0)
        except Exception:
            pass
        self._thread.join(timeout=2.0)
        self.node.destroy_node()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()
        return False
