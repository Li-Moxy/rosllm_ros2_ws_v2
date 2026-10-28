"""Observation manager: aggregates String topics and Observation sub-services.

Each returned observation carries a reserved metadata text element with
``status`` (fresh/stale/missing/error), ``age_sec`` and ``seq`` so that the
agent can reject missing or stale data instead of using it blindly.
"""

from __future__ import annotations

import json
import math
import signal
import re
import threading
import time
from pathlib import Path

import rclpy
from rclpy.executors import ExternalShutdownException
import yaml
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rosllm_msgs.msg import Observation as ObservationMsg
from rosllm_srvs.srv import Observation
from std_msgs.msg import String

from rosllm_core.action_client import CallStatus, ClientHost
from rosllm_core.observation import META_PREFIX

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class ObservationManagerNode(Node):
    def __init__(self):
        super().__init__("observation_manager")
        self.declare_parameter("config_filename", "")
        self.declare_parameter("staleness_sec", 3.0)
        self.declare_parameter("total_timeout_sec", 2.0)
        self.declare_parameter("max_text_bytes", 8192)
        self.declare_parameter("service_name", "get_observation")

        self.staleness = float(self.get_parameter("staleness_sec").value)
        self.total_timeout = float(self.get_parameter("total_timeout_sec").value)
        self.max_text_bytes = int(self.get_parameter("max_text_bytes").value)
        for name, value, low, high in (
            ("staleness_sec", self.staleness, 0.05, 3600.0),
            ("total_timeout_sec", self.total_timeout, 0.05, 60.0),
        ):
            if not math.isfinite(value) or not low <= value <= high:
                raise RuntimeError(f"parameter {name!r} must be within {low}..{high}")
        if not 128 <= self.max_text_bytes <= 65536:
            raise RuntimeError("parameter 'max_text_bytes' must be within 128..65536")

        config = self._load_config()
        self.staleness = float(config.get("staleness_sec", self.staleness))
        self._topic_specs = self._parse_topics(config.get("observations_topics", []))
        self._service_specs = self._parse_services(config.get("observations_srvs", []))

        self._latest: dict[str, tuple[float, int, str]] = {}
        self._seq = 0
        self._data_lock = threading.Lock()
        for name, topic in self._topic_specs.items():
            self.create_subscription(
                String,
                topic,
                lambda msg, _name=name: self._on_topic(_name, msg),
                10,
            )
        self.host = ClientHost("observation_manager_clients", num_threads=3)
        for spec in self._service_specs.values():
            self.host.create_client(spec["service"], Observation)

        self._group = ReentrantCallbackGroup()
        service_name = str(self.get_parameter("service_name").value)
        self.create_service(
            Observation, service_name, self._handle, callback_group=self._group
        )
        self.get_logger().info(
            "observation manager ready: topics=%s services=%s staleness=%.2fs"
            % (list(self._topic_specs), [s["service"] for s in self._service_specs.values()], self.staleness)
        )

    # ------------------------------------------------------------ config
    def _load_config(self) -> dict:
        path = str(self.get_parameter("config_filename").value).strip()
        if not path:
            raise RuntimeError("parameter 'config_filename' is required")
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"cannot read observation config {path!r}: {exc}") from exc
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RuntimeError(f"invalid YAML in observation config: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("observation config root must be a mapping")
        unknown = sorted(
            set(data) - {"staleness_sec", "observations_topics", "observations_srvs"}
        )
        if unknown:
            raise RuntimeError(f"unknown observation config key(s) {unknown!r}")
        if "staleness_sec" in data:
            value = data["staleness_sec"]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuntimeError("staleness_sec must be numeric")
            if not math.isfinite(float(value)) or not 0.05 <= float(value) <= 3600.0:
                raise RuntimeError("staleness_sec must be within 0.05..3600")
        return data

    def _parse_topics(self, raw) -> dict[str, str]:
        if raw is None:
            return {}
        if not isinstance(raw, list) or len(raw) > 32:
            raise RuntimeError("observations_topics must be a list of at most 32 items")
        result: dict[str, str] = {}
        for entry in raw:
            if isinstance(entry, str):
                name, topic = entry, entry
            elif isinstance(entry, dict):
                unknown = set(entry) - {"name", "topic"}
                if unknown:
                    raise RuntimeError(f"observation topic has unknown key(s) {unknown!r}")
                name = entry.get("name")
                topic = entry.get("topic", name)
            else:
                raise RuntimeError("each observation topic must be a string or mapping")
            if not isinstance(name, str) or not _NAME_RE.match(name):
                raise RuntimeError(f"invalid observation name {name!r}")
            if not isinstance(topic, str) or not topic.strip() or len(topic) > 256:
                raise RuntimeError(f"invalid topic for observation {name!r}")
            if name in result:
                raise RuntimeError(f"duplicate observation name {name!r}")
            result[name] = topic
        return result

    def _parse_services(self, raw) -> dict[str, dict]:
        if raw is None:
            return {}
        if not isinstance(raw, list) or len(raw) > 32:
            raise RuntimeError("observations_srvs must be a list of at most 32 items")
        result: dict[str, dict] = {}
        for entry in raw:
            if isinstance(entry, str):
                name = service = entry
                timeout = 1.0
            elif isinstance(entry, dict):
                unknown = set(entry) - {"name", "service", "timeout_sec"}
                if unknown:
                    raise RuntimeError(f"observation service has unknown key(s) {unknown!r}")
                name = entry.get("name")
                service = entry.get("service", name)
                timeout = entry.get("timeout_sec", 1.0)
            else:
                raise RuntimeError("each observation service must be a string or mapping")
            if not isinstance(name, str) or not _NAME_RE.match(name):
                raise RuntimeError(f"invalid observation name {name!r}")
            if not isinstance(service, str) or not service.strip() or len(service) > 255:
                raise RuntimeError(f"invalid service name for observation {name!r}")
            if not service.startswith("/"):
                service = "/" + service
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise RuntimeError(f"timeout for observation {name!r} must be numeric")
            if not math.isfinite(float(timeout)) or not 0.05 <= float(timeout) <= 10.0:
                raise RuntimeError(f"timeout for observation {name!r} must be 0.05..10")
            if name in result:
                raise RuntimeError(f"duplicate observation name {name!r}")
            result[name] = {"service": service, "timeout_sec": float(timeout)}
        return result

    # ------------------------------------------------------------ runtime
    def _on_topic(self, name: str, message: String) -> None:
        with self._data_lock:
            self._seq += 1
            self._latest[name] = (time.monotonic(), self._seq, message.data)

    @staticmethod
    def _meta(status: str, age: float | None, seq: int | None, source: str, detail: str = "") -> str:
        payload = {
            "status": status,
            "age_sec": None if age is None else round(max(0.0, age), 6),
            "seq": seq,
            "source": source,
        }
        if detail:
            payload["detail"] = detail
        return META_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _topic_observation(self, name: str, topic: str) -> ObservationMsg:
        observation = ObservationMsg()
        observation.name = name
        with self._data_lock:
            latest = self._latest.get(name)
        if latest is None:
            observation.text = [
                self._meta("missing", None, None, "topic", f"no message received on {topic}")
            ]
            return observation
        received, seq, data = latest
        age = time.monotonic() - received
        status = "fresh" if age <= self.staleness else "stale"
        detail = ""
        encoded = data.encode("utf-8")
        if len(encoded) > self.max_text_bytes:
            data = encoded[: self.max_text_bytes].decode("utf-8", "ignore")
            detail = "payload truncated"
        observation.text = [
            data,
            self._meta(status, age, seq, "topic", detail),
        ]
        return observation

    def _service_observation(self, name: str, spec: dict, deadline: float) -> list:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            observation = ObservationMsg()
            observation.name = name
            observation.text = [
                self._meta("error", None, None, "service", "observation total timeout expired")
            ]
            return [observation]
        result = self.host.call(
            spec["service"],
            Observation.Request(),
            min(spec["timeout_sec"], remaining),
            request_id=f"observation:{name}",
        )
        if result.status is not CallStatus.OK:
            status = "missing" if result.status is CallStatus.UNAVAILABLE else "error"
            observation = ObservationMsg()
            observation.name = name
            observation.text = [
                self._meta(status, None, None, "service", f"{result.status.value}: {result.error}")
            ]
            return [observation]
        if result.response.observation:
            chosen = [
                entry for entry in result.response.observation if entry.name == name
            ] or list(result.response.observation)
            for entry in chosen:
                if not entry.name:
                    entry.name = name
                if not any(text.startswith(META_PREFIX) for text in entry.text):
                    entry.text.append(
                        self._meta("fresh", result.elapsed_sec, None, "service")
                    )
            return chosen
        observation = ObservationMsg()
        observation.name = name
        observation.text = [
            self._meta("missing", None, None, "service", "service returned no observation")
        ]
        return [observation]

    def _handle(self, request, response):
        started = time.monotonic()
        deadline = started + self.total_timeout
        observations = []
        for name, topic in self._topic_specs.items():
            observations.append(self._topic_observation(name, topic))
        for name, spec in self._service_specs.items():
            observations.extend(self._service_observation(name, spec, deadline))
        response.observation = observations
        self.get_logger().debug(
            f"get_observation returned {len(observations)} entries in "
            f"{time.monotonic() - started:.3f}s"
        )
        return response

    def shutdown(self) -> None:
        self.host.shutdown()


def main() -> None:
    rclpy.init()
    node = None
    executor = None
    try:
        node = ObservationManagerNode()
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
