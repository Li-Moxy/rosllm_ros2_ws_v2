"""CLI entry point for the ROSLLM agent."""

from __future__ import annotations

import signal
import argparse
import json
import sys

import rclpy
from rclpy.utilities import remove_ros_args

from .agent import TaskReport


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Unofficial ROSLLM ROS 2 agent: task -> observation -> LLM plan -> "
            "execution -> observed-effect check."
        )
    )
    parser.add_argument("--task", help="run one task and exit")
    parser.add_argument("--json", action="store_true", help="print one JSON report")
    parser.add_argument("--library", help="override action_library_path")
    parser.add_argument("--llm-service", help="override llm_service")
    parser.add_argument("--execute-service", help="override execute_service")
    parser.add_argument("--observation-service", help="override observation_service")
    parser.add_argument("--max-steps", type=int, help="override max_steps")
    parser.add_argument("--expert-advice", help="override expert_advice_path")
    return parser


def _overrides(args) -> dict:
    mapping = {}
    if args.library:
        mapping["action_library_path"] = args.library
    if args.llm_service:
        mapping["llm_service"] = args.llm_service
    if args.execute_service:
        mapping["execute_service"] = args.execute_service
    if args.observation_service:
        mapping["observation_service"] = args.observation_service
    if args.max_steps is not None:
        mapping["max_steps"] = args.max_steps
    if args.expert_advice:
        mapping["expert_advice_path"] = args.expert_advice
    return mapping


def _print_report(report: TaskReport) -> None:
    print(f"status: {report.status.value} (exit {report.exit_code})")
    print(f"plan_id: {report.plan_id}")
    print(f"message: {report.message}")
    if report.plan:
        print("plan: " + json.dumps(report.plan, ensure_ascii=False))
    if report.verification:
        for check in report.verification:
            print(
                "verify: {action} {observation}.{field} -> {ok} ({reason})".format(
                    **check
                )
            )
    if report.observations:
        print("observations:\n" + report.observations)


def main() -> int:
    argv = remove_ros_args(sys.argv)
    args = _parser().parse_args(argv[1:])

    rclpy.init(args=sys.argv)
    node = None
    try:
        from .node import RosAgentNode

        node = RosAgentNode(_overrides(args))
        if args.task is not None:
            report = node.run_task(args.task)
            if args.json:
                print(json.dumps(report.to_dict(), ensure_ascii=False))
            else:
                _print_report(report)
            return report.exit_code

        print("ROSLLM agent ready. Enter a task, or 'q' to quit.")
        while rclpy.ok():
            try:
                task = input("task> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if task.lower() in ("q", "quit", "exit"):
                break
            if not task:
                continue
            report = node.run_task(task)
            if args.json:
                print(json.dumps(report.to_dict(), ensure_ascii=False))
            else:
                _print_report(report)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - report configuration errors cleanly
        print(f"ros_agent error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
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
    sys.exit(main())
