"""Cross-process, plan-level mutual exclusion.

Only one plan may control a robot at a time. An uncertain action leaves a
recovery marker in the shared file, surviving executor shutdown/restart.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path


class LeaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class LeaseResult:
    granted: bool
    holder: dict | None = None
    reason: str = ""


class PlanLease:
    def __init__(self, robot_id: str = "default", directory: str = "/tmp"):
        self._fd: int | None = None
        if not robot_id or len(robot_id) > 128:
            raise LeaseError("robot_id must be a non-empty string of <= 128 characters")
        digest = hashlib.sha256(robot_id.encode("utf-8")).hexdigest()[:16]
        self.robot_id = robot_id
        self.path = Path(directory) / f"rosllm_plan_{digest}.lock"
        self._fd: int | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LeaseError(f"cannot create lease directory: {exc}") from exc

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self, owner: str, note: str = "") -> LeaseResult:
        if self._fd is not None:
            return LeaseResult(False, self._read_holder(), "this executor already holds a lease")
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise LeaseError(f"cannot open lease file {self.path}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = self._read_holder()
            os.close(fd)
            return LeaseResult(False, holder, "another plan is executing")
        except OSError as exc:
            os.close(fd)
            raise LeaseError(f"cannot lock lease file: {exc}") from exc

        holder = self._read_holder()
        if holder and holder.get("recovery_required"):
            os.close(fd)
            return LeaseResult(False, holder, "recovery_required")

        payload = {
            "owner": owner,
            "pid": os.getpid(),
            "since": time.time(),
            "note": note,
            "robot_id": self.robot_id,
        }
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")[:2048]
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, data)
        except OSError:
            # Lock is still valid even if diagnostics could not be written.
            pass
        self._fd = fd
        return LeaseResult(True, payload, "")

    def mark_uncertain(self, reason: str) -> None:
        """Persist unknown execution state while this plan still owns the lock."""
        if self._fd is None:
            raise LeaseError("cannot mark uncertainty without owning the lease")
        payload = self._read_holder() or {"robot_id": self.robot_id}
        payload.update(recovery_required=True, reason=reason)
        try:
            self._write_holder(self._fd, payload)
        except OSError as exc:
            raise LeaseError(f"cannot persist recovery state: {exc}") from exc

    def confirm_stopped(self, owner: str) -> LeaseResult:
        """Caller attests the robot is stopped; never clear an active plan's lock.

        This is an acknowledgement, not a remote cancellation or a sensor check.
        The same flock serialises acknowledgement and new plan acquisition.
        """
        if self.held:
            return LeaseResult(False, reason="a plan is still executing")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return LeaseResult(False, reason="a plan is still executing")
            self._write_holder(fd, {
                "robot_id": self.robot_id,
                "recovery_required": False,
                "confirmed_by": owner,
                "confirmed_at": time.time(),
            })
            return LeaseResult(True, reason="stopped state acknowledged")
        except OSError as exc:
            raise LeaseError(f"cannot acknowledge stopped state: {exc}") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _write_holder(fd: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "wb") as stream:
            stream.write(data)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _read_holder(self) -> dict | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def __del__(self):  # pragma: no cover - best effort
        self.release()
