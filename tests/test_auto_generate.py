
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

import pytest

from lab_scheduler.compliance import MANITOBA, ONTARIO, ShiftTemplateInfo
from lab_scheduler.engine.constraints import build_coverage_targets_from_roster
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    auto_generate_schedule,
    validate_generated_schedule,
)
from lab_scheduler.simulation.hospital_stress import shift_required_qualifications, shift_templates
from lab_scheduler.workers.logic_worker import LogicWorkerFailure

from portage_fixtures import portage_generate_kwargs


def _templates() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
        "shift-evening": ShiftTemplateInfo(
            "shift-evening", "EVENING", "Evening", "15:00", "23:00", 480, False
        ),
        "shift-night": ShiftTemplateInfo(
            "shift-night", "NIGHT", "Night", "23:00", "07:00", 480, True
        ),
    }


def _employees() -> list[EmployeeProfile]:
    return [
        EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-b1", "Jordan Patel", 0.8, {"qual-mlt"}),
        EmployeeProfile("emp-c1", "Riley Chen", 0.6, {"qual-mla"}),
    ]


def _required() -> dict[str, set[str]]:
    return {
        "shift-morning": {"qual-mlt", "qual-mla"},
        "shift-evening": {"qual-mlt"},
        "shift-night": {"qual-mlt"},
    }


def test_auto_generate_fills_slots_without_compliance_errors() -> None:
    kwargs = portage_generate_kwargs()
    result = auto_generate_schedule(**kwargs)
    assert result.slots_filled > 0
    assert result.fill_rate_pct > 0

    validate_generated_schedule(
        result,
        rules=kwargs["rules"],
        employees=kwargs["employees"],
        shift_templates=kwargs["shift_templates"],
        period_start=kwargs["period_start"],
        period_end=kwargs["period_end"],
        weeks_in_period=kwargs["weeks_in_period"],
    )


def test_auto_generate_respects_qualifications() -> None:
    kwargs = portage_generate_kwargs(
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
    )
    result = auto_generate_schedule(**kwargs)
    required = kwargs["shift_required_qualifications"]
    employees_by_id = {employee.id: employee for employee in kwargs["employees"]}

    for assignment in result.assignments:
        assert assignment.shift_template_id in required
        employee = employees_by_id[assignment.employee_id]
        assert employee.qualification_ids & required[assignment.shift_template_id]


def test_auto_generate_marks_labor_rule_gaps_as_hard_failures() -> None:
    employees = [
        EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"}, seniority_hours=9000.0),
        EmployeeProfile("emp-b1", "Jordan Patel", 1.0, {"qual-mlt"}, seniority_hours=8000.0),
    ]
    targets = build_coverage_targets_from_roster(
        employees,
        qual_codes={"qual-mlt": "MLT"},
    )
    try:
        auto_generate_schedule(
            rules=MANITOBA,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 7),
            weeks_in_period=1,
            employees=employees,
            shift_templates=shift_templates(),
            shift_required_qualifications=shift_required_qualifications(),
            coverage_targets=targets,
        )
    except LogicWorkerFailure as exc:
        assert exc.error in {
            ScheduleError.LABOR_RULE,
            ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
            ScheduleError.COVERAGE_TARGET,
        }


def test_auto_generate_fails_when_understaffed() -> None:
    with pytest.raises(LogicWorkerFailure):
        auto_generate_schedule(
            rules=MANITOBA,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 14),
            weeks_in_period=2,
            employees=_employees(),
            shift_templates=_templates(),
            shift_required_qualifications=_required(),
            coverage_targets=build_coverage_targets_from_roster(
                _employees(),
                qual_codes={"qual-mlt": "MLT", "qual-mla": "MLA"},
            ),
        )
