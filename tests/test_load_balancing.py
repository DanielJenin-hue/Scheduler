
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

import pytest

from lab_scheduler.engine.demand import resolve_seats_for_shift, portage_concurrent_demands
from lab_scheduler.scheduling.auto_generate import (
    ImmediateClinicalFailure,
    PlannedAssignment,
    _enforce_weekend_qual_limits,
)
from lab_scheduler.scheduling.load_balancing import (
    WeekdayDailyStaffingPlan,
    compute_weekday_daily_staffing_plan,
    weekday_daily_staffing_hard_block,
    weekday_daily_staffing_rank_penalty,
    weekday_day_shift_capacity_block,
    weekday_morning_shift_count_from_states,
    weekend_qual_cap_reached,
    weekend_qual_counts_from_assignments,
    prune_weekend_assignments_to_cap,
)
from lab_scheduler.engine.demand import WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT
from lab_scheduler.scheduling.profiles import EmployeeProfile


def test_prune_weekend_assignments_to_cap() -> None:
    from lab_scheduler.scheduling.auto_generate import PlannedAssignment, _EmployeeState

    saturday = date(2026, 6, 6)
    employees = [
        EmployeeProfile("emp-mlt-1", "MLT One", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-mlt-2", "MLT Two", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-mla-1", "MLA One", 1.0, {"qual-mla"}),
        EmployeeProfile("emp-mla-2", "MLA Two", 1.0, {"qual-mla"}),
    ]
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    shift_templates = {
        "shift-morning": type("T", (), {"id": "shift-morning", "code": "MORNING", "duration_minutes": 480})(),
    }
    assignments = [
        PlannedAssignment("emp-mlt-1", "shift-morning", saturday),
        PlannedAssignment("emp-mlt-2", "shift-morning", saturday),
        PlannedAssignment("emp-mla-1", "shift-morning", saturday),
        PlannedAssignment("emp-mla-2", "shift-morning", saturday),
    ]
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=160.0)
        for employee in employees
    }
    removed = prune_weekend_assignments_to_cap(
        assignments,
        states=states,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=shift_templates,
        period_start=saturday,
        period_end=saturday,
    )
    assert removed == 1
    assert len(assignments) == 3
    weekend_counts = weekend_qual_counts_from_assignments(
        assignments,
        employees=employees,
        qual_codes=qual_codes,
        assignment_date=saturday,
    )
    assert weekend_counts == {"MLT": 2, "MLA": 1}


def test_prune_weekend_assignments_to_cap_includes_vacant_portage_lines() -> None:
    from lab_scheduler.scheduling.auto_generate import _EmployeeState

    saturday = date(2026, 6, 6)
    employees = [
        EmployeeProfile("emp-mla-1", "MLA One", 1.0, {"qual-mla"}),
        EmployeeProfile(
            "vacant-01",
            "Vacant MLA D/E - Line 01",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "vacant-02",
            "Vacant MLA D/E - Line 02",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
    ]
    qual_codes = {"qual-mla": "MLA"}
    shift_templates = {
        "shift-morning": type("T", (), {"id": "shift-morning", "code": "MORNING", "duration_minutes": 480})(),
    }
    assignments = [
        PlannedAssignment("emp-mla-1", "shift-morning", saturday),
        PlannedAssignment("vacant-01", "shift-morning", saturday),
        PlannedAssignment("vacant-02", "shift-morning", saturday),
    ]
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=320.0)
        for employee in employees
    }
    removed = prune_weekend_assignments_to_cap(
        assignments,
        states=states,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=shift_templates,
        period_start=saturday,
        period_end=saturday,
    )
    assert removed == 2
    assert len(assignments) == 1
    assert assignments[0].employee_id == "emp-mla-1"
    saturday = date(2026, 6, 6)
    evening = resolve_seats_for_shift(
        saturday,
        "EVENING",
        portage_concurrent_demands(),
    )
    night = resolve_seats_for_shift(
        saturday,
        "NIGHT",
        portage_concurrent_demands(),
    )
    morning = resolve_seats_for_shift(
        saturday,
        "MORNING",
        portage_concurrent_demands(),
    )
    assert len(evening) == 2
    assert len(night) == 2
    assert len(morning) == 2


def test_weekend_qual_cap_reached() -> None:
    counts = {"MLT": 1, "MLA": 0}
    assert weekend_qual_cap_reached(counts, "MLT") is False
    assert weekend_qual_cap_reached(counts, "MLA") is False
    assert weekend_qual_cap_reached({"MLT": 2, "MLA": 0}, "MLT") is True
    assert weekend_qual_cap_reached({"MLT": 0, "MLA": 1}, "MLA") is True


def test_enforce_weekend_qual_limits_rejects_over_cap() -> None:
    saturday = date(2026, 6, 6)
    employees = [
        EmployeeProfile("emp-mlt-1", "MLT One", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-mlt-2", "MLT Two", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-mla-1", "MLA One", 1.0, {"qual-mla"}),
    ]
    assignments = [
        PlannedAssignment("emp-mlt-1", "shift-morning", saturday),
        PlannedAssignment("emp-mlt-2", "shift-morning", saturday),
        PlannedAssignment("emp-mla-1", "shift-morning", saturday),
    ]
    shift_templates = {
        "shift-morning": type("T", (), {"id": "shift-morning", "code": "MORNING", "duration_minutes": 480})(),
    }
    with pytest.raises(ImmediateClinicalFailure, match="cap exceeded"):
        _enforce_weekend_qual_limits(
            assignments,
            employees=employees,
            qual_codes={"qual-mlt": "MLT", "qual-mla": "MLA"},
            shift_templates=shift_templates,
            period_start=saturday,
            period_end=saturday,
        )


def test_weekday_daily_staffing_penalty_prefers_under_target_days() -> None:
    plan = WeekdayDailyStaffingPlan(target_per_day=10.0, weekday_count=20)
    light_day_penalty = weekday_daily_staffing_rank_penalty(
        date(2026, 6, 1),
        current_count=8,
        plan=plan,
    )
    heavy_day_penalty = weekday_daily_staffing_rank_penalty(
        date(2026, 6, 2),
        current_count=12,
        plan=plan,
    )
    assert light_day_penalty < heavy_day_penalty


def test_weekday_daily_staffing_hard_block_at_target() -> None:
    plan = WeekdayDailyStaffingPlan(target_per_day=13.0, weekday_count=20)
    assert weekday_daily_staffing_hard_block(date(2026, 6, 1), 12, plan) is False
    assert weekday_daily_staffing_hard_block(date(2026, 6, 1), 13, plan) is True
    assert weekday_daily_staffing_hard_block(date(2026, 6, 6), 20, plan) is False


def test_compute_weekday_daily_staffing_plan_uses_operational_target() -> None:
    employees = [
        EmployeeProfile("emp-1", "One", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-2", "Two", 1.0, {"qual-mla"}),
    ]

    class _Rules:
        standard_hours_per_week_at_1_0_fte = 40.0

    plan = compute_weekday_daily_staffing_plan(
        employees,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        standard_weekly_hours=_Rules.standard_hours_per_week_at_1_0_fte,
        weeks_in_period=4,
        shift_hours=8.0,
    )
    assert plan.weekday_count == 20
    assert plan.target_per_day == pytest.approx(16.0)


def test_weekday_day_shift_capacity_block_at_fourteen() -> None:
    monday = date(2026, 6, 1)
    assert weekday_day_shift_capacity_block(
        monday,
        WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT - 1,
        shift_code="MORNING",
    ) is False
    assert weekday_day_shift_capacity_block(
        monday,
        WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT,
        shift_code="MORNING",
    ) is True
    assert weekday_day_shift_capacity_block(
        monday,
        20,
        shift_code="EVENING",
    ) is False
    saturday = date(2026, 6, 6)
    assert weekday_day_shift_capacity_block(
        saturday,
        WEEKDAY_DAY_SHIFT_CAPACITY_LIMIT,
        shift_code="MORNING",
    ) is False


def test_weekday_morning_shift_count_from_states() -> None:
    from lab_scheduler.scheduling.auto_generate import PlannedAssignment, _EmployeeState

    monday = date(2026, 6, 1)
    employees = [
        EmployeeProfile("emp-1", "One", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-2", "Two", 1.0, {"qual-mla"}),
    ]
    shift_templates = {
        "shift-morning": type("T", (), {"id": "shift-morning", "code": "MORNING", "duration_minutes": 480})(),
        "shift-evening": type("T", (), {"id": "shift-evening", "code": "EVENING", "duration_minutes": 480})(),
    }
    states = {
        employees[0].id: _EmployeeState(profile=employees[0], target_hours=320.0),
        employees[1].id: _EmployeeState(profile=employees[1], target_hours=320.0),
    }
    states[employees[0].id].assignment_records.append((monday, "shift-morning"))
    states[employees[1].id].assignment_records.append((monday, "shift-evening"))
    assert weekday_morning_shift_count_from_states(
        states,
        monday,
        shift_templates=shift_templates,
    ) == 1
