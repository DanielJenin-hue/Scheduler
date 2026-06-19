
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.scheduling.auto_generate import auto_generate_schedule
from lab_scheduler.workers.logic_worker import (
    LogicWorkerPayload,
    LogicWorkerStatus,
    build_logic_worker_output,
    run_logic_worker,
)
from portage_fixtures import portage_generate_kwargs


def test_auto_generate_emit_triage_collects_open_slots_without_raising() -> None:
    kwargs = portage_generate_kwargs(strict_complete_block=True)
    result = auto_generate_schedule(**kwargs, emit_triage=True)

    assert result.assignments
    assert result.triage_list
    output = build_logic_worker_output(result)
    payload = output.to_dict()

    assert output.status is LogicWorkerStatus.PARTIAL_SUCCESS
    assert payload["status"] == "PARTIAL_SUCCESS"
    assert payload["assignments"]
    assert payload["triage_list"]
    assert all(entry["deficit_hours"] == 8.0 for entry in payload["triage_list"])
    assert all(
        entry["error_code"] in {
            ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
            ScheduleError.LABOR_RULE.value,
        }
        for entry in payload["triage_list"]
    )


def test_run_logic_worker_with_portage_emit_triage_returns_partial_success() -> None:
    kwargs = portage_generate_kwargs(
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        strict_complete_block=True,
    )

    output = run_logic_worker(
        LogicWorkerPayload(period_start=date(2026, 6, 1)),
        generate=lambda _acceptance: auto_generate_schedule(**kwargs, emit_triage=True),
    )

    payload = output.to_dict()
    assert payload["status"] in {"SUCCESS", "PARTIAL_SUCCESS"}
    assert "assignments" in payload
    assert "triage_list" in payload
