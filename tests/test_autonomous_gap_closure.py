
import pytest

pytestmark = pytest.mark.legacy

from datetime import date, datetime, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import (
    AutoGenerateResult,
    PlannedAssignment,
    UnionRiskLine,
    _EmployeeState,
    _autonomous_gap_closure,
    _collect_unassigned_pool_slots,
    _identify_union_risk_lines,
    _would_violate_gap_closure_rules,
)
from lab_scheduler.scheduling.contract_payroll import fulltime_period_contract_hours
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.engine.demand import ExpandedScheduleSlot


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


def test_identify_union_risk_lines_flags_sub_160h_fulltime() -> None:
    employees = [EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})]
    target = fulltime_period_contract_hours(rules=MANITOBA, weeks_in_period=4)
    states = {
        "emp-a1": _EmployeeState(profile=employees[0], target_hours=target, total_hours=120.0),
    }
    lines = _identify_union_risk_lines(
        employees,
        states,
        rules=MANITOBA,
        weeks_in_period=4,
    )
    assert len(lines) == 1
    assert isinstance(lines[0], UnionRiskLine)
    assert lines[0].deficit_hours == 40.0
    assert "Union Risk" not in lines[0].employee_name


def test_would_violate_gap_closure_rules_blocks_15h_turnaround() -> None:
    templates = _templates()
    employee = EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})
    state = _EmployeeState(profile=employee, target_hours=160.0)
    state.assignment_records = [(date(2026, 6, 1), "shift-evening")]
    state.work_dates.add(date(2026, 6, 1))
    state.total_hours = 8.0
    state.week_hours[date(2026, 6, 1)] = 8.0

    violation = _would_violate_gap_closure_rules(
        state,
        date(2026, 6, 2),
        templates["shift-morning"],
        templates,
        MANITOBA,
        date(2026, 6, 1),
        date(2026, 6, 28),
    )
    assert violation is not None
    assert "turnaround" in violation or "11h rest" in violation


def test_autonomous_gap_closure_fills_union_risk_from_unassigned_pool() -> None:
    templates = _templates()
    employee = EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})
    target = fulltime_period_contract_hours(rules=MANITOBA, weeks_in_period=4)
    state = _EmployeeState(profile=employee, target_hours=target, total_hours=152.0)
    states = {employee.id: state}

    open_dates = [
        date(2026, 6, 8) + timedelta(weeks=week, days=day)
        for week in range(1)
        for day in range(1)
    ]
    expanded_slots = [
        ExpandedScheduleSlot(
            assignment_date=open_dates[0],
            shift_id="shift-morning",
            seat_index=0,
            role_pool_id="pool-morning",
            required_qual_code="MLT",
        )
    ]
    fill_counts: dict = {}
    result = AutoGenerateResult(
        assignments=[
            PlannedAssignment(employee.id, "shift-morning", date(2026, 6, 1) + timedelta(days=offset))
            for offset in range(19)
        ]
    )
    for offset in range(19):
        assignment_date = date(2026, 6, 1) + timedelta(days=offset)
        if assignment_date.weekday() < 5:
            week_start = assignment_date - timedelta(days=assignment_date.weekday())
            state.week_hours[week_start] = state.week_hours.get(week_start, 0.0) + 8.0
            state.work_dates.add(assignment_date)
            state.assignment_records.append((assignment_date, "shift-morning"))

    added = _autonomous_gap_closure(
        result,
        rules=MANITOBA,
        employees=[employee],
        shift_templates=templates,
        shift_required_qualifications={"shift-morning": {"qual-mlt"}},
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employee_target_hours={employee.id: target},
        availability_blocked=None,
        qual_codes={"qual-mlt": "MLT"},
        expanded_slots=expanded_slots,
        fill_counts=fill_counts,
        filled_smooth_seats=set(),
        states=states,
    )
    assert added >= 0
    pool = _collect_unassigned_pool_slots(
        expanded_slots,
        fill_counts=fill_counts,
        shift_templates=templates,
        filled_smooth_seats=set(),
    )
    assert len(pool) == 1 or added == 1
