"""Template sensor summarizer (ported from upstream, without the LLM call).

Upstream ``general_sensor_summarizer`` is not usable as published: it contains a
syntax error (missing comma in ``rospy.Timer(...)``), references an undefined
``deepcopy`` and assigns ``req.prompt.data`` although ``LLM.srv`` declares a
plain string.  This node keeps only the deterministic template summarizer that
the upstream launch file actually uses.
"""

from __future__ import annotations

import math
import signal
from collections import deque
from pathlib import Path

import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from std_msgs.msg import Float64MultiArray, String

TEMPLATE = """# Sensor
{description}
The past {n} measurement{s}
{history}
"""


def render_summary(description: str, history) -> str:
    values = list(history)
    n = len(values)
    return TEMPLATE.format(
        description=description,
        n=n,
        s="s" if n != 1 else "",
        history="\n".join(str(value) for value in values),
    )


class TemplateSummarizerNode(Node):
    def __init__(self):
        super().__init__("template_summarizer")
        self.declare_parameter("max_history", 1)
        self.declare_parameter("sensor_description_path", "")
        self.declare_parameter("hz", 1.0)
        self.max_history = int(self.get_parameter("max_history").value)
        if not 1 <= self.max_history <= 1000:
            raise RuntimeError("parameter 'max_history' must be within 1..1000")
        self.hz = float(self.get_parameter("hz").value)
        if not math.isfinite(self.hz) or not 0.05 <= self.hz <= 100.0:
            raise RuntimeError("parameter 'hz' must be within 0.05..100")
        path = str(self.get_parameter("sensor_description_path").value).strip()
        if not path:
            raise RuntimeError("parameter 'sensor_description_path' is required")
        try:
            self.description = Path(path).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"cannot read sensor description: {exc}") from exc
        if len(self.description.encode("utf-8")) > 16384:
            self.description = self.description[:16384]

        self.history = deque(maxlen=self.max_history)
        self.create_subscription(
            Float64MultiArray, "sensor_data", self._on_data, 10
        )
        self.publisher = self.create_publisher(String, "sensor_summary", 10)
        self.create_timer(1.0 / self.hz, self._publish)
        self.get_logger().info(
            f"template summarizer ready: max_history={self.max_history} hz={self.hz}"
        )

    def _on_data(self, message: Float64MultiArray) -> None:
        values = []
        for value in message.data:
            if isinstance(value, float) and not math.isfinite(value):
                self.get_logger().warning("dropping non-finite sensor sample")
                return
            values.append(float(value))
        self.history.append(values)

    def _publish(self) -> None:
        message = String()
        message.data = render_summary(self.description, self.history)
        self.publisher.publish(message)


def main() -> None:
    rclpy.init()
    node = None
    try:
        node = TemplateSummarizerNode()
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
