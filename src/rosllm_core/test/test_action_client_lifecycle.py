import os
import random

import pytest
import rclpy
from rclpy.impl.implementation_singleton import rclpy_implementation

from rosllm_core.action_client import ClientHost


def test_shutdown_destroys_node_and_is_idempotent():
    os.environ["ROS_DOMAIN_ID"] = str(random.randint(10, 220))
    os.environ["ROS_LOCALHOST_ONLY"] = "1"
    rclpy.init()
    host = ClientHost("lifecycle_regression")
    try:
        host.shutdown()
        host.shutdown()
        with pytest.raises(rclpy_implementation.InvalidHandle):
            host.node.get_name()
    finally:
        host.shutdown()
        rclpy.shutdown()
