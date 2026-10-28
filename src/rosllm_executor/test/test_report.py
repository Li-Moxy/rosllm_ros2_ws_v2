from rosllm_core.info import Info
from rosllm_core.report import (
    SCHEMA,
    ExecStatus,
    ExecutionReport,
    StepRecord,
    parse_report,
    report_json,
    status_for_info,
)


def test_report_round_trip():
    report = ExecutionReport(
        status=ExecStatus.OK,
        info=int(Info.OK),
        message="done",
        plan_id="p1",
        robot_id="r1",
        total_steps=2,
        completed_steps=2,
        elapsed_sec=1.234,
        steps=[
            StepRecord(
                index=0,
                name="forward",
                status="ok",
                info=0,
                output="{}",
                elapsed_sec=1.0,
                request_id="p1:s0",
                observation={"name": "pose", "status": "fresh", "payload": {"x": 1.0}},
            )
        ],
    )
    parsed = parse_report(report_json(report))
    assert parsed is not None
    assert parsed.status is ExecStatus.OK and parsed.plan_id == "p1"
    assert parsed.steps[0].observation["payload"]["x"] == 1.0
    assert parsed.completed_steps == 2


def test_parse_foreign_or_bad_reports():
    assert parse_report("") is None
    assert parse_report("not json") is None
    assert parse_report('{"success": true}') is None
    assert parse_report('{"schema": "other"}') is None
    assert parse_report('{"schema": "%s", "status": "bogus"}' % SCHEMA) is None


def test_parse_tolerates_missing_fields():
    parsed = parse_report('{"schema": "%s", "status": "timeout"}' % SCHEMA)
    assert parsed is not None
    assert parsed.status is ExecStatus.TIMEOUT
    assert parsed.total_steps == 0 and parsed.steps == []


def test_status_mapping():
    assert status_for_info(int(Info.OK)) is ExecStatus.OK
    assert status_for_info(int(Info.INVALID_BEHAVIOR)) is ExecStatus.INVALID
    assert status_for_info(int(Info.BUSY)) is ExecStatus.BUSY
    assert status_for_info(int(Info.TIMEOUT)) is ExecStatus.TIMEOUT
    assert status_for_info(int(Info.UNKNOWN_STATE)) is ExecStatus.UNKNOWN
    assert status_for_info(int(Info.ATOMIC_ACTION_UNAVAILABLE)) is ExecStatus.UNAVAILABLE
    assert status_for_info(int(Info.LIMIT_EXCEEDED)) is ExecStatus.LIMIT
    assert status_for_info(int(Info.ERROR_ATOMIC_ACTION)) is ExecStatus.FAILED
