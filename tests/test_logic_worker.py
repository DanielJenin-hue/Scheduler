from datetime import date
from unittest.mock import MagicMock

import pytest

from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.workers.logic_worker import (
    GenerationTriageSink,
    LogicWorkerFailure,
    LogicWorkerOutput,
    LogicWorkerPayload,
    LogicWorkerRejection,
    LogicWorkerStatus,
    TriageEntry,
    append_triage_entry,
    build_logic_worker_output,
    infer_blocked_by_rule,
    require_complete_assignment_block,
    require_monday_block_start,
    resolve_logic_worker_status,
    run_logic_worker,
)


def test_require_monday_block_start_accepts_monday() -> None:
    monday = date(2026, 6, 1)
    assert require_monday_block_start(monday) == monday


def test_require_monday_block_start_rejects_non_monday() -> None:
    tuesday = date(2026, 6, 2)
    with pytest.raises(LogicWorkerRejection) as exc_info:
        require_monday_block_start(tuesday)

    rejection = exc_info.value
    assert rejection.error is ScheduleError.ERR_NON_MONDAY_BLOCK_START
    assert rejection.period_start == tuesday
    assert rejection.expected_monday == date(2026, 6, 1)
    assert rejection.to_dict()["error_code"] == ScheduleError.ERR_NON_MONDAY_BLOCK_START.value


def test_run_logic_worker_rejects_before_context_loader() -> None:
    loader = MagicMock(return_value="should-not-run")

    with pytest.raises(LogicWorkerRejection):
        run_logic_worker(
            LogicWorkerPayload(period_start=date(2026, 6, 2)),
            load_context=loader,
        )

    loader.assert_not_called()


def test_run_logic_worker_accepts_monday_without_loader() -> None:
    result = run_logic_worker(LogicWorkerPayload(period_start=date(2026, 6, 1)))
    assert result.block_start_monday == date(2026, 6, 1)


def test_run_logic_worker_rejects_before_generate_callback() -> None:
    generate = MagicMock()

    with pytest.raises(LogicWorkerRejection):
        run_logic_worker(
            LogicWorkerPayload(period_start=date(2026, 6, 2)),
            generate=generate,
        )

    generate.assert_not_called()


def test_build_logic_worker_output_partial_success() -> None:
    class _Partial:
        assignments = [
            PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 1)),
        ]
        triage_list = [
            TriageEntry(
                slot_id="2026-06-12|EVENING|shift-evening|Clinical Floor - Evening - Seat_02|seat=1|qual=MLT",
                slot="Vacant MLT D/E - Line 03",
                assignment_date=date(2026, 6, 12),
                error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                blocked_by=ScheduleError.MAX_WEEKLY_HOURS,
                deficit_hours=8.0,
                shift_code="EVENING",
            )
        ]

    output = build_logic_worker_output(_Partial())
    payload = output.to_dict()

    assert output.status is LogicWorkerStatus.PARTIAL_SUCCESS
    assert payload["status"] == "PARTIAL_SUCCESS"
    assert len(payload["assignments"]) == 1
    assert payload["triage_list"] == [
        {
            "slot_id": (
                "2026-06-12|EVENING|shift-evening|Clinical Floor - Evening - Seat_02|seat=1|qual=MLT"
            ),
            "slot": "Vacant MLT D/E - Line 03",
            "date": "2026-06-12",
            "error_code": ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
            "failed_rule_code": ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
            "blocked_by": ScheduleError.MAX_WEEKLY_HOURS.value,
            "deficit_hours": 8.0,
            "shift_code": "EVENING",
        }
    ]
    assert payload["shift_equity_metrics"] == {}


def test_build_logic_worker_output_includes_shift_equity_metrics() -> None:
    class _Result:
        assignments = [
            PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 1)),
        ]
        triage_list = []
        shift_equity_metrics = {
            "MLT_D_N_Pool": {
                "target_avg_nights": 18,
                "line_01": {"total_D": 22, "total_N": 18, "variance_from_avg": "0"},
            }
        }

    output = build_logic_worker_output(_Result())
    assert output.shift_equity_metrics["MLT_D_N_Pool"]["target_avg_nights"] == 18
    assert output.to_dict()["shift_equity_metrics"]["MLT_D_N_Pool"]["line_01"]["total_N"] == 18


def test_require_complete_assignment_block_returns_partial_success_payload() -> None:
    class _Partial:
        assignments = [
            PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 1)),
        ]
        triage_list = [
            TriageEntry(
                slot_id="slot-1",
                slot="Vacant MLA D/N - Line 04",
                assignment_date=date(2026, 6, 18),
                error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                blocked_by=ScheduleError.CONTRACT_FTE_160,
                deficit_hours=8.0,
            )
        ]

    output = require_complete_assignment_block(_Partial())
    assert isinstance(output, LogicWorkerOutput)
    assert output.status is LogicWorkerStatus.PARTIAL_SUCCESS


def test_require_complete_assignment_block_raises_when_no_assignments() -> None:
    class _Empty:
        assignments = []
        triage_list = [
            TriageEntry(
                slot_id="slot-1",
                slot="Vacant MLT D/N - Line 01",
                assignment_date=date(2026, 6, 12),
                error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                blocked_by=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                deficit_hours=8.0,
            )
        ]

    with pytest.raises(LogicWorkerFailure):
        require_complete_assignment_block(_Empty())


def test_infer_blocked_by_rule_maps_contract_and_weekly_hours() -> None:
    assert (
        infer_blocked_by_rule(
            slot_is_impossible=False,
            qualified_staff_exist=True,
            constraint_summary="would exceed 160h contract target",
        )
        is ScheduleError.CONTRACT_FTE_160
    )
    assert (
        infer_blocked_by_rule(
            slot_is_impossible=False,
            qualified_staff_exist=True,
            ineligible_reasons={"emp-a": "weekly hours would exceed 40.0"},
        )
        is ScheduleError.MAX_WEEKLY_HOURS
    )


def test_append_triage_entry_populates_sink() -> None:
    sink = GenerationTriageSink()
    append_triage_entry(
        sink,
        assignment_date=date(2026, 6, 12),
        shift_code="EVENING",
        shift_id="shift-evening",
        role_pool_id="Clinical Floor - Evening - Seat_02",
        seat_index=2,
        required_qual_code="MLT",
        shift_hours=8.0,
        slot_is_impossible=False,
        qualified_staff_exist=True,
        ineligible_reasons={"emp-a": "weekly hours would exceed 40.0"},
    )

    assert len(sink.entries) == 1
    assert sink.entries[0].slot.startswith("Vacant MLT")
    assert sink.entries[0].blocked_by is ScheduleError.MAX_WEEKLY_HOURS


def test_resolve_logic_worker_status() -> None:
    assert (
        resolve_logic_worker_status(
            assignments=[object()],
            triage_list=[
                TriageEntry(
                    slot_id="x",
                    slot="Vacant MLT D/N - Line 01",
                    assignment_date=date(2026, 6, 1),
                    error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                    blocked_by=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                    deficit_hours=8.0,
                )
            ],
        )
        is LogicWorkerStatus.PARTIAL_SUCCESS
    )
    assert resolve_logic_worker_status(assignments=[object()], triage_list=[]) is LogicWorkerStatus.SUCCESS


def test_run_logic_worker_accepts_monday_then_loads_context() -> None:
    loader = MagicMock(return_value={"loaded": True})

    loaded = run_logic_worker(
        LogicWorkerPayload(
            period_start=date(2026, 6, 1),
            tenant_id="demo-tenant",
            schedule_period_id="period-2026-summer",
        ),
        load_context=loader,
    )

    loader.assert_called_once()
    assert loaded == {"loaded": True}


def test_run_logic_worker_returns_json_contract_from_generate_callback() -> None:
    class _Generated:
        assignments = [
            PlannedAssignment("emp-a1", "shift-morning", date(2026, 6, 1)),
        ]
        triage_list = [
            TriageEntry(
                slot_id="slot-1",
                slot="Vacant MLT D/N - Line 03",
                assignment_date=date(2026, 6, 12),
                error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                blocked_by=ScheduleError.MAX_WEEKLY_HOURS,
                deficit_hours=8.0,
            )
        ]

    output = run_logic_worker(
        LogicWorkerPayload(period_start=date(2026, 6, 1)),
        generate=lambda _acceptance: _Generated(),
    )

    assert isinstance(output, LogicWorkerOutput)
    assert output.to_dict()["status"] == "PARTIAL_SUCCESS"
