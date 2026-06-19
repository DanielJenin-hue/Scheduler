from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lab_scheduler.audit.triage_escalation import write_triage_escalation_report
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.scheduling.auto_generate import PlannedAssignment, auto_generate_schedule
from lab_scheduler.workers.export_worker import ExportWorkerInput, run_export_worker
from lab_scheduler.workers.logic_worker import (
    LogicWorkerOutput,
    LogicWorkerPayload,
    LogicWorkerStatus,
    TriageEntry,
    run_logic_worker,
)
from lab_scheduler.workers.orchestrator import route_logic_worker_output
from portage_fixtures import portage_generate_kwargs


def test_write_triage_escalation_uses_dated_filename(tmp_path: Path) -> None:
    output = LogicWorkerOutput(
        status=LogicWorkerStatus.PARTIAL_SUCCESS,
        assignments=(
            {
                "employee_id": "emp-a1",
                "shift_template_id": "shift-morning",
                "assignment_date": "2026-06-01",
            },
        ),
        triage_list=(
            TriageEntry(
                slot_id="slot-1",
                slot="Vacant MLT D/N - Line 03",
                assignment_date=date(2026, 6, 12),
                error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                blocked_by=ScheduleError.MAX_WEEKLY_HOURS,
                deficit_hours=8.0,
            ),
        ),
    )

    path = write_triage_escalation_report(
        tmp_path,
        output,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        report_date=date(2026, 5, 27),
    )

    assert path == tmp_path / "exports" / "Triage_Escalation_2026-05-27.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "PARTIAL_SUCCESS"
    assert payload["triage_count"] == 1
    assert payload["triage_list"][0]["blocked_by"] == "MAX_WEEKLY_HOURS"


def test_orchestrator_partial_success_writes_triage_and_continues(tmp_path: Path) -> None:
    output = LogicWorkerOutput(
        status=LogicWorkerStatus.PARTIAL_SUCCESS,
        assignments=(
            {
                "employee_id": "portage-mlt-01",
                "shift_template_id": "shift-morning",
                "assignment_date": "2026-06-01",
            },
        ),
        triage_list=(
            TriageEntry(
                slot_id="slot-1",
                slot="Vacant MLA D/N - Line 04",
                assignment_date=date(2026, 6, 18),
                error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                blocked_by=ScheduleError.CONTRACT_FTE_160,
                deficit_hours=8.0,
            ),
        ),
    )

    result = route_logic_worker_output(
        tmp_path,
        output,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        report_date=date(2026, 5, 27),
    )

    assert result.halted is False
    assert result.logic_status is LogicWorkerStatus.PARTIAL_SUCCESS
    assert result.triage_escalation_path == "exports/Triage_Escalation_2026-05-27.json"
    assert (tmp_path / result.triage_escalation_path).is_file()
    assert result.export_result is not None
    assert result.export_result.triage_escalation_path == result.triage_escalation_path
    assert result.export_result.assignment_count == 1

    export_payload = json.loads(result.export_result.export_path.read_text(encoding="utf-8"))
    assert export_payload["triage_escalation_path"] == result.triage_escalation_path
    assert len(export_payload["assignments"]) == 1


def test_orchestrator_success_skips_triage_file(tmp_path: Path) -> None:
    output = LogicWorkerOutput(
        status=LogicWorkerStatus.SUCCESS,
        assignments=(
            {
                "employee_id": "portage-mlt-01",
                "shift_template_id": "shift-morning",
                "assignment_date": "2026-06-01",
            },
        ),
        triage_list=(),
    )

    result = route_logic_worker_output(
        tmp_path,
        output,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        report_date=date(2026, 5, 27),
    )

    assert result.halted is False
    assert result.triage_escalation_path is None
    assert list((tmp_path / "exports").glob("Triage_Escalation_*.json")) == []
    assert result.export_result is not None
    assert result.export_result.triage_escalation_path is None


def test_orchestrator_failure_halts_without_export(tmp_path: Path) -> None:
    output = LogicWorkerOutput(
        status=LogicWorkerStatus.FAILURE,
        assignments=(),
        triage_list=(
            TriageEntry(
                slot_id="slot-1",
                slot="Vacant MLT D/N - Line 01",
                assignment_date=date(2026, 6, 12),
                error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                blocked_by=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                deficit_hours=8.0,
            ),
        ),
    )
    export_worker = MagicMock()

    result = route_logic_worker_output(
        tmp_path,
        output,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        export_worker=export_worker,
    )

    assert result.halted is True
    assert result.export_result is None
    export_worker.assert_not_called()


def test_export_worker_receives_triage_path(tmp_path: Path) -> None:
    result = run_export_worker(
        tmp_path,
        ExportWorkerInput(
            assignments=[
                {
                    "employee_id": "emp-a1",
                    "shift_template_id": "shift-morning",
                    "assignment_date": date(2026, 6, 1),
                }
            ],
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 7),
            triage_escalation_path="exports/Triage_Escalation_2026-05-27.json",
        ),
        report_date=date(2026, 5, 27),
    )

    payload = json.loads(result.export_path.read_text(encoding="utf-8"))
    assert payload["triage_escalation_path"] == "exports/Triage_Escalation_2026-05-27.json"
    assert result.export_path.name == "Schedule_Export_2026-05-27.json"


def test_end_to_end_orchestrator_routes_logic_worker_partial_success(tmp_path: Path) -> None:
    kwargs = portage_generate_kwargs(
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        strict_complete_block=True,
    )

    logic_output = run_logic_worker(
        LogicWorkerPayload(period_start=date(2026, 6, 1)),
        generate=lambda _acceptance: auto_generate_schedule(**kwargs, emit_triage=True),
    )

    pipeline = route_logic_worker_output(
        tmp_path,
        logic_output,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        report_date=date(2026, 5, 27),
    )

    assert pipeline.halted is False
    if logic_output.status is LogicWorkerStatus.PARTIAL_SUCCESS:
        assert pipeline.triage_escalation_path is not None
        triage_payload = json.loads(
            (tmp_path / pipeline.triage_escalation_path).read_text(encoding="utf-8")
        )
        assert triage_payload["triage_list"]
    assert pipeline.export_result is not None
    assert pipeline.export_result.assignment_count == len(logic_output.assignments)
