"""Machine-readable execution reports carried in ``ExecuteBehavior.message``.

The upstream service only has ``bool success``, ``string message`` and
``int32 info``.  This port keeps those fields and stores a structured JSON
document in ``message`` so the agent can distinguish explicit failure from
timeout, unknown state and busy rejection, and can perform per-step
verification.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum

from .info import Info
from .jsonutil import StrictJSONError, dumps, loads

SCHEMA = "rosllm.execute.v1"


class ExecStatus(str, Enum):
    OK = "ok"
    INVALID = "invalid"
    BUSY = "busy"
    TIMEOUT = "timeout"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"
    LIMIT = "limit"


def status_for_info(info: int) -> ExecStatus:
    if info == Info.OK:
        return ExecStatus.OK
    if info in (Info.NULL_BEHAVIOR, Info.INVALID_BEHAVIOR, Info.FAILED_TO_COMPILE):
        return ExecStatus.INVALID
    if info == Info.BUSY:
        return ExecStatus.BUSY
    if info == Info.TIMEOUT:
        return ExecStatus.TIMEOUT
    if info == Info.ATOMIC_ACTION_UNAVAILABLE:
        return ExecStatus.UNAVAILABLE
    if info == Info.UNKNOWN_STATE:
        return ExecStatus.UNKNOWN
    if info == Info.LIMIT_EXCEEDED:
        return ExecStatus.LIMIT
    return ExecStatus.FAILED


@dataclass
class StepRecord:
    index: int | None
    name: str
    status: str = "pending"
    info: int = int(Info.OK)
    output: str = ""
    error: str = ""
    elapsed_sec: float = 0.0
    request_id: str = ""
    observation: dict | None = None

    def to_dict(self) -> dict:
        data = {
            "index": self.index,
            "name": self.name,
            "status": self.status,
            "info": self.info,
            "elapsed_sec": round(self.elapsed_sec, 6),
            "request_id": self.request_id,
        }
        if self.output:
            data["output"] = self.output
        if self.error:
            data["error"] = self.error
        if self.observation is not None:
            data["observation"] = self.observation
        return data


@dataclass
class ExecutionReport:
    status: ExecStatus
    info: int
    message: str
    plan_id: str = ""
    robot_id: str = ""
    total_steps: int = 0
    completed_steps: int = 0
    elapsed_sec: float = 0.0
    steps: list[StepRecord] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.status is ExecStatus.OK

    def to_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "plan_id": self.plan_id,
            "robot_id": self.robot_id,
            "status": self.status.value,
            "info": self.info,
            "message": self.message,
            "total_steps": self.total_steps,
            "completed_steps": self.completed_steps,
            "elapsed_sec": round(self.elapsed_sec, 6),
            "steps": [step.to_dict() for step in self.steps],
        }

    def to_json(self) -> str:
        return dumps(self.to_dict())


def parse_report(text: str) -> ExecutionReport | None:
    """Parse our structured report; return ``None`` for foreign/legacy messages."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data = loads(text)
    except StrictJSONError:
        return None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return None
    status_value = data.get("status")
    try:
        status = ExecStatus(status_value)
    except (TypeError, ValueError):
        return None
    info = data.get("info", int(Info.OK))
    if isinstance(info, bool) or not isinstance(info, int):
        info = int(Info.UNKNOWN_STATE)
    steps = []
    raw_steps = data.get("steps")
    if isinstance(raw_steps, list):
        for raw in raw_steps:
            if not isinstance(raw, dict):
                continue
            index = raw.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                index = None
            name = raw.get("name")
            if not isinstance(name, str):
                name = ""
            elapsed = raw.get("elapsed_sec")
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
                elapsed = 0.0
            step_info = raw.get("info", int(Info.OK))
            if isinstance(step_info, bool) or not isinstance(step_info, int):
                step_info = int(Info.UNKNOWN_STATE)
            observation = raw.get("observation")
            if not isinstance(observation, dict):
                observation = None
            steps.append(
                StepRecord(
                    index=index,
                    name=name,
                    status=str(raw.get("status", "")),
                    info=step_info,
                    output=str(raw.get("output", "")),
                    error=str(raw.get("error", "")),
                    elapsed_sec=float(elapsed),
                    request_id=str(raw.get("request_id", "")),
                    observation=observation,
                )
            )
    elapsed = data.get("elapsed_sec")
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        elapsed = 0.0
    total = data.get("total_steps")
    completed = data.get("completed_steps")
    if isinstance(total, bool) or not isinstance(total, int):
        total = len(steps)
    if isinstance(completed, bool) or not isinstance(completed, int):
        completed = sum(1 for step in steps if step.status == "ok")
    return ExecutionReport(
        status=status,
        info=info,
        message=str(data.get("message", "")),
        plan_id=str(data.get("plan_id", "")),
        robot_id=str(data.get("robot_id", "")),
        total_steps=total,
        completed_steps=completed,
        elapsed_sec=float(elapsed),
        steps=steps,
    )


def report_json(report: ExecutionReport) -> str:
    """Serialise a report for ``ExecuteBehavior.message`` (never raises)."""
    try:
        return report.to_json()
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        return json.dumps(
            {
                "schema": SCHEMA,
                "status": report.status.value,
                "info": report.info,
                "message": f"{report.message} (report serialisation failed: {exc})",
            }
        )
