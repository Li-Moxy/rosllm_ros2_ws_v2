import multiprocessing
import time

import pytest

from rosllm_core.lease import LeaseError, PlanLease


def test_acquire_and_release(tmp_path):
    lease = PlanLease("robot-a", str(tmp_path))
    result = lease.try_acquire("executor-1", "plan-1")
    assert result.granted and lease.held
    assert result.holder["owner"] == "executor-1"
    second = lease.try_acquire("executor-2", "plan-2")
    assert not second.granted and lease.held
    lease.release()
    assert not lease.held
    assert lease.try_acquire("executor-2", "plan-2").granted
    lease.release()


def test_two_instances_conflict(tmp_path):
    first = PlanLease("robot-a", str(tmp_path))
    second = PlanLease("robot-a", str(tmp_path))
    assert first.try_acquire("first").granted
    blocked = second.try_acquire("second")
    assert not blocked.granted
    assert blocked.holder is not None
    assert "another plan" in blocked.reason
    first.release()
    assert second.try_acquire("second").granted
    second.release()


def test_different_robots_are_independent(tmp_path):
    a = PlanLease("robot-a", str(tmp_path))
    b = PlanLease("robot-b", str(tmp_path))
    assert a.try_acquire("a").granted
    assert b.try_acquire("b").granted
    a.release()
    b.release()


def test_invalid_robot_id(tmp_path):
    with pytest.raises(LeaseError):
        PlanLease("", str(tmp_path))
    with pytest.raises(LeaseError):
        PlanLease("x" * 129, str(tmp_path))


def test_uncertain_state_survives_release_and_new_instance(tmp_path):
    lease = PlanLease("uncertain-robot", str(tmp_path))
    assert lease.try_acquire("first", "plan-1").granted
    lease.mark_uncertain("remote action timed out")
    lease.release()
    replacement = PlanLease("uncertain-robot", str(tmp_path))
    result = replacement.try_acquire("replacement")
    assert not result.granted and result.reason == "recovery_required"
    assert result.holder["reason"] == "remote action timed out"
    assert replacement.confirm_stopped("operator").granted
    assert replacement.try_acquire("after-confirmation").granted
    replacement.release()


def test_confirmation_cannot_unlock_an_active_plan(tmp_path):
    first = PlanLease("active-robot", str(tmp_path))
    second = PlanLease("active-robot", str(tmp_path))
    assert first.try_acquire("first").granted
    assert not first.confirm_stopped("same-process").granted
    assert not second.confirm_stopped("other-process").granted
    assert first.held
    first.release()


def test_mark_uncertain_requires_ownership(tmp_path):
    lease = PlanLease("robot", str(tmp_path))
    with pytest.raises(LeaseError, match="owning"):
        lease.mark_uncertain("no active plan")


def _hold_in_child(directory, queue):
    lease = PlanLease("child-robot", directory)
    granted = lease.try_acquire("child").granted
    queue.put(granted)
    time.sleep(1.0)
    lease.release()


def test_process_death_releases_lock(tmp_path):
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    process = context.Process(target=_hold_in_child, args=(str(tmp_path), queue))
    process.start()
    try:
        assert queue.get(timeout=5.0) is True
        parent = PlanLease("child-robot", str(tmp_path))
        assert not parent.try_acquire("parent").granted
        process.join(timeout=5.0)
        assert process.exitcode == 0
        assert parent.try_acquire("parent").granted
        parent.release()
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2.0)
