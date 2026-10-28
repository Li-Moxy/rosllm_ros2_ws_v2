"""Turtlesim demo launch (offline fake LLM by default).

ROS domain isolation is deliberately left to the caller: run with
``ROS_DOMAIN_ID=77`` (or pass ``domain_id:=77``) and never connect this demo to
a physical robot.
"""

from __future__ import annotations

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    OpaqueFunction,
    SetEnvironmentVariable,
)
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node


def _launch_setup(context, *args, **kwargs):
    library = LaunchConfiguration("action_library").perform(context)
    observation_config = LaunchConfiguration("observation_config").perform(context)
    fake_response = LaunchConfiguration("fake_response").perform(context)
    env_file = LaunchConfiguration("env_file").perform(context)
    domain_id = LaunchConfiguration("domain_id").perform(context).strip()
    headless = LaunchConfiguration("headless").perform(context).lower() in (
        "1",
        "true",
        "yes",
    )
    executor = LaunchConfiguration("executor").perform(context)
    if executor not in ("sequence", "behavior_tree"):
        raise RuntimeError("executor must be 'sequence' or 'behavior_tree'")

    actions = []
    # Safety default: never silently share ROS domain 0 with a real robot or
    # another experiment.  An explicit non-zero environment/launch value is
    # respected (the integration tests use a random domain).
    if not domain_id:
        domain_id = os.environ.get("ROS_DOMAIN_ID", "").strip()
    if not domain_id or domain_id == "0":
        domain_id = "77"
        actions.append(SetEnvironmentVariable("ROS_DOMAIN_ID", domain_id))
        print(
            "[rosllm_turtlesim] ROS_DOMAIN_ID was unset or 0; "
            "using 77 for this offline demo"
        )
    else:
        actions.append(SetEnvironmentVariable("ROS_DOMAIN_ID", domain_id))
    if headless:
        actions.append(SetEnvironmentVariable("QT_QPA_PLATFORM", "offscreen"))

    actions.append(
        Node(
            package="turtlesim",
            executable="turtlesim_node",
            name="turtlesim",
            output="screen",
            condition=IfCondition(LaunchConfiguration("start_turtlesim")),
        )
    )
    actions.append(
        Node(
            package="rosllm_turtlesim",
            executable="turtle_actions",
            name="turtle_actions",
            output="screen",
            parameters=[
                {
                    "service_prefix": "/turtle1",
                    "cmd_vel_topic": "/turtle1/cmd_vel",
                    "pose_topic": "/turtle1/pose",
                    "max_linear_vel": 2.0,
                    "max_angular_vel": 2.0,
                    "move_duration_sec": 1.0,
                    "publish_hz": 20.0,
                    "stop_timeout_sec": 1.0,
                }
            ],
        )
    )
    actions.append(
        Node(
            package="rosllm_turtlesim",
            executable="turtle_observation",
            name="turtle_observation",
            output="screen",
            parameters=[
                {
                    "pose_topic": "/turtle1/pose",
                    "observation_topic": "/turtle1/observation",
                    "rate_hz": 10.0,
                }
            ],
        )
    )
    actions.append(
        Node(
            package="rosllm_observation",
            executable="observation_manager",
            name="observation_manager",
            output="screen",
            parameters=[
                {
                    "config_filename": observation_config,
                    "service_name": "get_observation",
                    "total_timeout_sec": 2.0,
                }
            ],
        )
    )
    actions.append(
        Node(
            package="rosllm_agent",
            executable="fake_llm_node",
            name="fake_llm",
            output="screen",
            parameters=[{"path": fake_response, "delay_sec": 0.0}],
            condition=IfCondition(LaunchConfiguration("use_fake_llm")),
        )
    )
    actions.append(
        Node(
            package="rosllm_agent",
            executable="llm_node",
            name="llm",
            output="screen",
            parameters=[
                {
                    "model": LaunchConfiguration("model"),
                    "base_url": LaunchConfiguration("base_url"),
                    "temperature": LaunchConfiguration("temperature"),
                    "timeout_sec": LaunchConfiguration("llm_timeout_sec"),
                    "api_key_env": LaunchConfiguration("api_key_env"),
                    "env_file": env_file,
                }
            ],
            condition=UnlessCondition(LaunchConfiguration("use_fake_llm")),
        )
    )

    common = {
        "action_library_path": library,
        "robot_id": LaunchConfiguration("robot_id"),
        "step_timeout_sec": 10.0,
        "plan_timeout_sec": 60.0,
        "preflight_timeout_sec": 2.0,
        "observation_service": "/get_observation",
        "observation_timeout_sec": 3.0,
        "observation_settle_sec": 0.2,
        "max_steps": 8,
    }
    actions.append(
        Node(
            package="rosllm_executor",
            executable="sequence_executor",
            name="sequence_executor",
            output="screen",
            parameters=[common],
            condition=IfCondition(
                PythonExpression(
                    ["'", LaunchConfiguration("executor"), "' == 'sequence'"]
                )
            ),
        )
    )
    actions.append(
        Node(
            package="rosllm_executor",
            executable="behavior_tree_executor",
            name="behavior_tree_executor",
            output="screen",
            parameters=[
                common,
                {
                    "bt_max_nodes": 64,
                    "bt_max_depth": 12,
                    "bt_max_action_calls": 32,
                    "bt_max_runtime_sec": 60.0,
                },
            ],
            condition=IfCondition(
                PythonExpression(
                    ["'", LaunchConfiguration("executor"), "' == 'behavior_tree'"]
                )
            ),
        )
    )
    return actions


def generate_launch_description() -> LaunchDescription:
    share = get_package_share_directory("rosllm_turtlesim")
    return LaunchDescription(
        [
            DeclareLaunchArgument("use_fake_llm", default_value="true"),
            DeclareLaunchArgument("start_turtlesim", default_value="true"),
            DeclareLaunchArgument("headless", default_value="false"),
            DeclareLaunchArgument("domain_id", default_value=""),
            DeclareLaunchArgument("executor", default_value="sequence"),
            DeclareLaunchArgument("robot_id", default_value="turtlesim_demo"),
            DeclareLaunchArgument(
                "action_library",
                default_value=os.path.join(share, "config", "action_library.yaml"),
            ),
            DeclareLaunchArgument(
                "observation_config",
                default_value=os.path.join(share, "config", "observation_config.yaml"),
            ),
            DeclareLaunchArgument(
                "fake_response",
                default_value=os.path.join(share, "config", "fake_llm_response.txt"),
            ),
            DeclareLaunchArgument("model", default_value="deepseek-chat"),
            DeclareLaunchArgument(
                "base_url", default_value="https://api.deepseek.com/v1"
            ),
            DeclareLaunchArgument("temperature", default_value="0.8"),
            DeclareLaunchArgument("llm_timeout_sec", default_value="30.0"),
            DeclareLaunchArgument("api_key_env", default_value="DEEPSEEK_API_KEY"),
            DeclareLaunchArgument(
                "env_file",
                default_value=os.path.expanduser("~/.config/rosllm/llm.env"),
            ),
            OpaqueFunction(function=_launch_setup),
        ]
    )
