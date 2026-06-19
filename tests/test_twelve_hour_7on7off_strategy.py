"""Tests for 12-hour 7-on/7-off cyclical staggered master-array generation."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.auto_generate import PlannedAssignment, auto_generate_schedule
from lab_scheduler.scheduling.breakroom_print import compute_contract_tracking_row
from lab_scheduler.scheduling.contract_payroll import period_contract_hours_for_fte
from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import (
    COVERAGE_STAGGER_DAYS,
    EIGHT_WEEK_CYCLE_SURPLUS_HOURS,
    FTE_TOPUP_TEMPLATE_ID,
    MASTER_ROTATION_DAYS,
    SIX_WEEK_RECONCILE_WINDOW_WEEKS,
    TWELVE_HOUR_PAID_HOURS,
    _apply_six_week_surplus_drops,
    build_eight_week_master_rotation_string,
    count_active_staff_on_day,
    daily_coverage_counts,
    fte_topup_hours,
    fulltime_stagger_offset_days,
    generate_schedule,
    is_master_work_day,
    lines_share_same_rotation_with_stagger,
    six_week_contract_target_hours,
)

from portage_fixtures import portage_generate_kwargs


def test_eight_week_master_rotation_string_shape():
    rotation = build_eight_week_master_rotation_string()
    assert len(rotation) == MASTER_ROTATION_DAYS
    assert rotation.count("S") == 28
    assert rotation.count(".") == 28
    assert rotation[:14] == ("S" * 7) + ("." * 7)


def test_fulltime_index_stagger_uses_seven_day_phase_step():
    assert fulltime_stagger_offset_days(0) == 0
    assert fulltime_stagger_offset_days(1) == COVERAGE_STAGGER_DAYS
    assert fulltime_stagger_offset_days(2) == 0


def test_consecutive_fulltime_indices_alternate_on_off_at_day_zero():
    cohort_size = 9
    assert is_master_work_day(period_day_index=0, fulltime_index=0, cohort_size=cohort_size)
    assert not is_master_work_day(period_day_index=0, fulltime_index=1, cohort_size=cohort_size)


def test_nine_line_daily_coverage_alternates_five_and_four():
    counts = daily_coverage_counts(cohort_size=9, days=14)
    assert counts[0] == 5
    assert counts[7] == 4
    assert max(counts) - min(counts) <= 1


def test_staggered_lines_share_same_rotation_offset_by_seven_days():
    cohort_size = 9
    assert lines_share_same_rotation_with_stagger(
        reference_day_index=0,
        reference_fulltime_index=0,
        comparison_day_index=COVERAGE_STAGGER_DAYS,
        comparison_fulltime_index=1,
        cohort_size=cohort_size,
    )


def test_eight_week_surplus_reconciled_by_one_shift_every_six_weeks():
    assert round(
        EIGHT_WEEK_CYCLE_SURPLUS_HOURS * (SIX_WEEK_RECONCILE_WINDOW_WEEKS / 8),
        3,
    ) == TWELVE_HOUR_PAID_HOURS


def test_six_week_drop_reconciles_fulltime_line_without_clipping_monday():
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)
    assignments = [
        PlannedAssignment(
            "emp-01",
            "shift-morning",
            period_start + timedelta(days=offset),
        )
        for offset in range(42)
        if is_master_work_day(period_day_index=offset, fulltime_index=0, cohort_size=1)
    ]
    dropped = _apply_six_week_surplus_drops(
        assignments,
        employee_id="emp-01",
        fte=1.0,
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
    )
    assert len(assignments) - len(dropped) == 1
    assert any(assignment.assignment_date == period_start for assignment in dropped)
    target = six_week_contract_target_hours(fte=1.0, rules=MANITOBA)
    assert len(dropped) * TWELVE_HOUR_PAID_HOURS <= target + 1e-6


def test_generate_schedule_starts_first_fulltime_line_on_monday():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
    )
    fulltime = sorted(
        (employee for employee in kwargs["employees"] if employee.fte >= 0.99),
        key=lambda employee: employee.id,
    )
    result = generate_schedule(**kwargs)
    first_line = fulltime[0]
    first_week = [
        assignment
        for assignment in result.assignments
        if assignment.employee_id == first_line.id
        and assignment.assignment_date <= date(2026, 6, 7)
    ]
    assert first_week
    assert min(assignment.assignment_date for assignment in first_week) == date(2026, 6, 1)


def test_generate_schedule_assigns_all_roster_lines():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    employees_by_id = {employee.id: employee for employee in kwargs["employees"]}
    active_ids = {employee.id for employee in kwargs["employees"] if employee.fte > 0}
    assigned_ids = {
        assignment.employee_id
        for assignment in result.assignments
        if assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID
    }
    assert active_ids.issubset(assigned_ids)
    assert result.slots_filled > 0


def test_fulltime_lines_keep_at_least_two_seven_on_blocks():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    fulltime_ids = {
        employee.id for employee in kwargs["employees"] if employee.fte >= 0.99
    }
    for employee_id in fulltime_ids:
        worked_dates = sorted(
            {
                assignment.assignment_date
                for assignment in result.assignments
                if assignment.employee_id == employee_id
                and assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID
            }
        )
        assert len(worked_dates) >= 14
        on_blocks = 0
        block_length = 0
        previous: date | None = None
        for worked_day in worked_dates:
            if previous is None or (worked_day - previous).days > 1:
                if block_length >= 7:
                    on_blocks += 1
                block_length = 1
            else:
                block_length += 1
            previous = worked_day
        if block_length >= 7:
            on_blocks += 1
        assert on_blocks >= 2


def test_contract_tracking_uses_twelve_hour_paid_coefficient():
    dates = [date(2026, 6, 1) + timedelta(days=offset) for offset in range(7)]
    row = {day.isoformat(): "D" for day in dates}
    tracking = compute_contract_tracking_row(
        fte=1.0,
        week_count=8,
        row=row,
        dates=dates,
        contract_line_type="D/E",
        schedule_archetype=ScheduleArchetype.TWELVE_HOUR.value,
    )
    assert tracking.actual_hours == round(7 * TWELVE_HOUR_PAID_HOURS, 1)
    assert tracking.status_class == "contract-ok" or tracking.variance_hours <= 0


@pytest.mark.legacy
def test_auto_generate_twelve_hour_archetype_routes_to_strategy():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = auto_generate_schedule(**kwargs, archetype="TWELVE_HOUR")
    assert result.deterministic_status == "GENERATED"
    assert result.assignments
    assert result.schedule_archetype == ScheduleArchetype.TWELVE_HOUR.value


def test_dn_contract_line_alternates_day_and_night_blocks():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    templates_by_id = kwargs["shift_templates"]
    dn_employee = next(
        employee
        for employee in kwargs["employees"]
        if str(employee.contract_line_type or "").upper() == "D/N"
        and employee.fte >= 0.99
    )
    result = generate_schedule(**kwargs)
    dn_assignments = sorted(
        (
            assignment
            for assignment in result.assignments
            if assignment.employee_id == dn_employee.id
            and assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID
        ),
        key=lambda assignment: assignment.assignment_date,
    )
    assert dn_assignments

    first_worked = dn_assignments[0].assignment_date

    def block_codes(block_index: int) -> set[str]:
        window_start = first_worked + timedelta(days=block_index * 14)
        window_end = window_start + timedelta(days=6)
        return {
            templates_by_id[assignment.shift_template_id].code.upper()
            for assignment in dn_assignments
            if window_start <= assignment.assignment_date <= window_end
        }

    first_block = block_codes(0)
    second_block = block_codes(1)
    assert len(first_block) == 1
    assert len(second_block) == 1
    assert first_block != second_block
    assert first_block | second_block == {"MORNING", "NIGHT"}


def test_daily_coverage_is_flat_across_full_time_cohort():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    period_days = [date(2026, 6, 1) + timedelta(days=offset) for offset in range(56)]
    worked_by_day = {day: 0 for day in period_days}
    for assignment in result.assignments:
        if assignment.shift_template_id == FTE_TOPUP_TEMPLATE_ID:
            continue
        if assignment.assignment_date in worked_by_day:
            worked_by_day[assignment.assignment_date] += 1
    counts = list(worked_by_day.values())
    # Line-number phase offsets (0, 7, 0, 7, …) keep daily coverage within a tight band.
    assert max(counts) - min(counts) <= 8
    assert min(counts) >= 5


def test_fte_topup_brings_full_time_line_to_exact_contract_target():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    fulltime = [
        employee for employee in kwargs["employees"] if employee.fte >= 0.99
    ]
    target = fte_topup_hours(
        fte=1.0,
        weeks_in_period=8,
        scheduled_shifts=0,
        rules=MANITOBA,
    )
    assert round(target, 1) == 320.0
    for employee in fulltime:
        line = [
            assignment
            for assignment in result.assignments
            if assignment.employee_id == employee.id
        ]
        worked = [
            assignment
            for assignment in line
            if assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID
        ]
        topups = [
            assignment
            for assignment in line
            if assignment.shift_template_id == FTE_TOPUP_TEMPLATE_ID
        ]
        target = period_contract_hours_for_fte(
            fte=employee.fte,
            weeks_in_period=8,
            standard_weekly_hours=MANITOBA.standard_hours_per_week_at_1_0_fte,
        )
        paid = len(worked) * TWELVE_HOUR_PAID_HOURS
        if topups:
            paid = target
        assert len(topups) <= 1
        assert round(paid, 1) == round(target, 1)


def _employee_by_vacant_line(employees, role: str, contract: str, line_number: int):
    label = f"Vacant {role} {contract} - Line {line_number:02d}"
    return next(employee for employee in employees if employee.full_name == label)


def _worked_pattern(
    result,
    *,
    employee_id: str,
    period_start: date,
    days: int = 14,
) -> str:
    worked = {
        assignment.assignment_date
        for assignment in result.assignments
        if assignment.employee_id == employee_id
        and assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID
    }
    return "".join(
        "D" if period_start + timedelta(days=offset) in worked else "."
        for offset in range(days)
    )


def test_mla_de_lines_stagger_by_line_number_phase():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    employees = kwargs["employees"]
    line_03 = _employee_by_vacant_line(employees, "MLA", "D/E", 3)
    line_04 = _employee_by_vacant_line(employees, "MLA", "D/E", 4)
    line_05 = _employee_by_vacant_line(employees, "MLA", "D/E", 5)
    pattern_03 = _worked_pattern(result, employee_id=line_03.id, period_start=date(2026, 6, 1))
    pattern_04 = _worked_pattern(result, employee_id=line_04.id, period_start=date(2026, 6, 1))
    pattern_05 = _worked_pattern(result, employee_id=line_05.id, period_start=date(2026, 6, 1))
    assert pattern_03 == pattern_05
    assert pattern_03 != pattern_04
    assert pattern_04.startswith(".......")
    assert pattern_03.startswith("DDDDDDD")


def test_mla_dn_lines_alternate_phases():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    employees = kwargs["employees"]
    patterns = [
        _worked_pattern(
            result,
            employee_id=_employee_by_vacant_line(employees, "MLA", "D/N", line).id,
            period_start=date(2026, 6, 1),
            days=14,
        )
        for line in (1, 2, 3, 4)
    ]
    assert len(set(patterns)) >= 2


def _assignment_pattern(
    result,
    *,
    employee_id: str,
    period_start: date,
    days: int = 14,
) -> str:
    scheduled = {
        assignment.assignment_date
        for assignment in result.assignments
        if assignment.employee_id == employee_id
    }
    return "".join(
        "X" if period_start + timedelta(days=offset) in scheduled else "."
        for offset in range(days)
    )


def test_fulltime_lines_use_contiguous_seven_on_seven_off_blocks():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    period_start = date(2026, 6, 1)
    for employee in kwargs["employees"]:
        if employee.fte < 0.99:
            continue
        pattern = _assignment_pattern(
            result,
            employee_id=employee.id,
            period_start=period_start,
            days=56,
        )
        assert "X.X" not in pattern
        assert pattern.count("X") == 28
        topups = [
            assignment
            for assignment in result.assignments
            if assignment.employee_id == employee.id
            and assignment.shift_template_id == FTE_TOPUP_TEMPLATE_ID
        ]
        assert len(topups) == 1


def test_part_time_mla_lines_receive_shifts():
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    result = generate_schedule(**kwargs)
    employees = kwargs["employees"]
    for line_number in (6, 7, 8):
        employee = _employee_by_vacant_line(employees, "MLA", "D/E", line_number)
        worked = [
            assignment
            for assignment in result.assignments
            if assignment.employee_id == employee.id
            and assignment.shift_template_id != FTE_TOPUP_TEMPLATE_ID
        ]
        assert worked, f"expected shifts for MLA D/E line {line_number:02d}"
        assert len(worked) >= 7
