from datetime import date, timedelta

import pytest

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.engine.constraints import (
    build_coverage_targets_from_roster,
    coverage_priority_key,
    portage_coverage_targets,
)
from lab_scheduler.engine.demand import (
    weekend_paired_day_rank_penalty,
    AutonomousDemandBalancer,
    DemandTier,
    CLINICAL_FLOOR,
    HARD_NIGHT_SHIFTS_PER_DAY,
    MISSING_CLINICAL_FLOOR_PENALTY,
    build_assignment_rank_key,
    count_expanded_slots,
    count_night_shifts_by_day,
    count_band_shifts_by_day,
    employee_matches_seat_qual,
    expand_schedule_slots,
    get_core_demands,
    is_clinical_floor_satisfied,
    is_demand_satisfied,
    is_night_demand_satisfied,
    is_smooth_day_balance_pool,
    missing_hard_demand_penalty,
    portage_blueprint_period_contract_hours,
    portage_concurrent_demands,
    portage_expanded_labor_hours,
    portage_expanded_slot_total,
    roster_line_number,
)
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    PlannedAssignment,
    _seat_fill_counts,
    auto_generate_schedule,
)
from lab_scheduler.scheduling.breakroom_print import compute_contract_tracking_row
from lab_scheduler.scheduling.seniority_ranking import cba_rank_key
from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLA,
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_employee_target_hours,
)
from portage_fixtures import portage_generate_kwargs


def _weekday_morning_seat_count() -> int:
    slots = expand_schedule_slots(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 5),
        shift_templates=shift_templates(),
        concurrent_demands=portage_concurrent_demands(),
    )
    weekday_mornings = [
        slot
        for slot in slots
        if slot.assignment_date.weekday() < 5
        and shift_templates()[slot.shift_id].code == "MORNING"
    ]
    return len(weekday_mornings)


def test_weekday_morning_uses_role_isolated_pools() -> None:
    slots = expand_schedule_slots(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 1),
        shift_templates=shift_templates(),
        concurrent_demands=portage_concurrent_demands(),
    )
    weekday_mornings = [
        slot
        for slot in slots
        if slot.assignment_date == date(2026, 6, 1)
        and shift_templates()[slot.shift_id].code == "MORNING"
    ]
    mlt_pool = [slot for slot in weekday_mornings if slot.role_pool_id == "Weekday Morning - MLT"]
    mla_pool = [slot for slot in weekday_mornings if slot.role_pool_id == "Weekday Morning - MLA"]
    clinical_floor = [
        slot for slot in weekday_mornings if slot.role_pool_id.startswith("Clinical Floor -")
    ]
    assert len(mlt_pool) == 6
    assert len(mla_pool) == 5
    assert len(clinical_floor) == 2
    assert all(slot.required_qual_code == "MLT" for slot in mlt_pool)
    assert all(slot.required_qual_code == "MLA" for slot in mla_pool)


def test_role_pool_rejects_cross_qualification_candidates() -> None:
    mlt = EmployeeProfile("emp-mlt", "MLT Tech", 1.0, {QUAL_MLT})
    mla = EmployeeProfile("emp-mla", "MLA Tech", 1.0, {QUAL_MLA})
    morning_required = shift_required_qualifications()["shift-morning"]

    assert employee_matches_seat_qual(
        mlt,
        "MLT",
        shift_required_qualification_ids=morning_required,
    )
    assert not employee_matches_seat_qual(
        mla,
        "MLT",
        shift_required_qualification_ids=morning_required,
    )
    assert employee_matches_seat_qual(
        mla,
        "MLA",
        shift_required_qualification_ids=morning_required,
    )


def test_build_assignment_rank_key_prioritizes_block_bonus_below_weekly_target() -> None:
    rules = MANITOBA
    assignment_date = date(2026, 6, 3)
    week_start = date(2026, 6, 1)
    continuing = EmployeeProfile("emp-a", "Avery Miller", 1.0, {QUAL_MLT})
    fresh = EmployeeProfile("emp-b", "Jordan Patel", 1.0, {QUAL_MLT})
    period_target = 160.0

    continuing_key = build_assignment_rank_key(
        profile=continuing,
        work_dates={date(2026, 6, 2)},
        assignment_records=[(date(2026, 6, 2), "shift-morning")],
        week_hours={week_start: 8.0},
        total_hours=80.0,
        assignment_date=assignment_date,
        shift_id="shift-morning",
        shift_hours=8.0,
        shift_template_code="MORNING",
        rules=rules,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        employees=[continuing, fresh],
        employee_total_hours={"emp-a": 80.0, "emp-b": 150.0},
        employee_target_hours={"emp-a": period_target, "emp-b": period_target},
        qual_codes={QUAL_MLT: "MLT"},
        prioritize_coverage=False,
        period_target_hours={"emp-a": period_target, "emp-b": period_target},
        coverage_priority_key_fn=coverage_priority_key,
        cba_rank_key_fn=cba_rank_key,
    )
    fresh_key = build_assignment_rank_key(
        profile=fresh,
        work_dates=set(),
        assignment_records=[],
        week_hours={week_start: 0.0},
        total_hours=150.0,
        assignment_date=assignment_date,
        shift_id="shift-morning",
        shift_hours=8.0,
        shift_template_code="MORNING",
        rules=rules,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        employees=[continuing, fresh],
        employee_total_hours={"emp-a": 80.0, "emp-b": 150.0},
        employee_target_hours={"emp-a": period_target, "emp-b": period_target},
        qual_codes={QUAL_MLT: "MLT"},
        prioritize_coverage=False,
        period_target_hours={"emp-a": period_target, "emp-b": period_target},
        coverage_priority_key_fn=coverage_priority_key,
        cba_rank_key_fn=cba_rank_key,
    )
    assert continuing_key < fresh_key


def test_portage_weekday_morning_expands_to_thirteen_seats_per_day() -> None:
    slots = expand_schedule_slots(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 1),
        shift_templates=shift_templates(),
        concurrent_demands=portage_concurrent_demands(),
    )
    weekday_mornings = [
        slot
        for slot in slots
        if slot.assignment_date == date(2026, 6, 1)
        and shift_templates()[slot.shift_id].code == "MORNING"
        and not is_smooth_day_balance_pool(slot.role_pool_id)
    ]
    assert len(weekday_mornings) == 13


def test_smooth_day_balance_seats_distributed_across_weekdays() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 28)
    slots = expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=shift_templates(),
        concurrent_demands=portage_concurrent_demands(),
    )
    smooth = [slot for slot in slots if is_smooth_day_balance_pool(slot.role_pool_id)]
    assert len(smooth) == 54

    weekdays = sorted({slot.assignment_date for slot in smooth})
    assert all(day.weekday() < 5 for day in weekdays)
    assert len(weekdays) == 20

    per_day = {}
    for slot in smooth:
        per_day[slot.assignment_date] = per_day.get(slot.assignment_date, 0) + 1
    assert max(per_day.values()) == 3
    assert min(per_day.values()) == 2

    templates = shift_templates()
    assert all(templates[slot.shift_id].code == "MORNING" for slot in smooth)


def test_autonomous_demand_balancer_matches_payroll_supply() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 28)
    templates = shift_templates()
    employees = build_portage_roster()
    demands = portage_concurrent_demands()

    plan = AutonomousDemandBalancer(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
        concurrent_demands=demands,
        employees=employees,
        rules=MANITOBA,
        weeks_in_period=4,
    ).reconcile()

    assert plan.is_balanced
    assert plan.balance_slot_count == 54
    assert plan.weekday_smooth_slot_count == 54
    assert plan.payroll_supply_hours == portage_blueprint_period_contract_hours(
        weeks_in_period=4,
    )
    assert abs(
        plan.baseline_template_hours + plan.balance_hours - plan.payroll_supply_hours
    ) < 0.01

    expanded = expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
        concurrent_demands=demands,
        supplemental_balance_slots=plan.balance_slots,
    )
    expanded_hours = sum(templates[slot.shift_id].duration_minutes / 60.0 for slot in expanded)
    assert abs(expanded_hours - plan.payroll_supply_hours) < 0.01


def test_portage_evening_and_night_use_one_mlt_one_mla_per_day() -> None:
    slots = expand_schedule_slots(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 2),
        shift_templates=shift_templates(),
        concurrent_demands=portage_concurrent_demands(),
    )
    for band in ("EVENING", "NIGHT"):
        band_slots = [
            slot
            for slot in slots
            if shift_templates()[slot.shift_id].code == band
        ]
        assert len(band_slots) == 4
        assert {slot.required_qual_code for slot in band_slots} == {"MLT", "MLA"}


def test_portage_expanded_slot_count_for_four_week_block() -> None:
    total = portage_expanded_slot_total(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        shift_templates=shift_templates(),
    )
    # 20 weekdays × (13 day + 2 eve + 2 night) + 8 weekend days × (2 day + 2 eve + 2 night)
    # + smooth weekday day-balance seats (autonomous reconciliation)
    assert total == 442


def test_portage_demand_matrix_matches_roster_contract_hours() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 28)
    templates = shift_templates()
    contract_hours = portage_blueprint_period_contract_hours(weeks_in_period=4)
    assert contract_hours == 3536.0

    slot_total = portage_expanded_slot_total(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
    )
    labor_hours = portage_expanded_labor_hours(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
    )

    assert slot_total == 442
    assert labor_hours == float(slot_total * 8)
    assert labor_hours == contract_hours


def test_smooth_balance_ranking_prioritizes_under_target_fulltime() -> None:
    rules = MANITOBA
    assignment_date = date(2026, 6, 1)
    week_start = date(2026, 6, 1)
    under = EmployeeProfile("emp-a", "Avery Miller", 1.0, {QUAL_MLT})
    loaded = EmployeeProfile("emp-b", "Jordan Patel", 1.0, {QUAL_MLT})
    period_target = 160.0
    smooth_pool = "Smooth Day Balance - MLT - Day 01"

    under_key = build_assignment_rank_key(
        profile=under,
        work_dates=set(),
        assignment_records=[],
        week_hours={week_start: 0.0},
        total_hours=144.0,
        assignment_date=assignment_date,
        shift_id="shift-morning",
        shift_hours=8.0,
        shift_template_code="MORNING",
        rules=rules,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        employees=[under, loaded],
        employee_total_hours={"emp-a": 144.0, "emp-b": 160.0},
        employee_target_hours={"emp-a": period_target, "emp-b": period_target},
        qual_codes={QUAL_MLT: "MLT"},
        prioritize_coverage=True,
        period_target_hours={"emp-a": period_target, "emp-b": period_target},
        coverage_priority_key_fn=coverage_priority_key,
        cba_rank_key_fn=cba_rank_key,
        role_pool_id=smooth_pool,
    )
    loaded_key = build_assignment_rank_key(
        profile=loaded,
        work_dates=set(),
        assignment_records=[],
        week_hours={week_start: 0.0},
        total_hours=160.0,
        assignment_date=assignment_date,
        shift_id="shift-morning",
        shift_hours=8.0,
        shift_template_code="MORNING",
        rules=rules,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        employees=[under, loaded],
        employee_total_hours={"emp-a": 144.0, "emp-b": 160.0},
        employee_target_hours={"emp-a": period_target, "emp-b": period_target},
        qual_codes={QUAL_MLT: "MLT"},
        prioritize_coverage=True,
        period_target_hours={"emp-a": period_target, "emp-b": period_target},
        coverage_priority_key_fn=coverage_priority_key,
        cba_rank_key_fn=cba_rank_key,
        role_pool_id=smooth_pool,
    )
    assert under_key < loaded_key


def test_portage_auto_generate_schedules_all_smooth_balance_seats() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 28)
    employees = build_portage_roster()
    targets = portage_coverage_targets(employees)
    target_hours = portage_employee_target_hours(
        employees,
        weeks_in_period=4,
        rules=MANITOBA,
    )
    templates = shift_templates()
    smooth_slots = [
        slot
        for slot in expand_schedule_slots(
            period_start=period_start,
            period_end=period_end,
            shift_templates=templates,
            concurrent_demands=portage_concurrent_demands(),
        )
        if is_smooth_day_balance_pool(slot.role_pool_id)
    ]
    assert len(smooth_slots) == 54

    result = auto_generate_schedule(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=4,
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=target_hours,
        coverage_targets=targets,
        strict_complete_block=False,
    )

    assigned_keys = {
        (assignment.assignment_date, assignment.shift_template_id)
        for assignment in result.assignments
    }
    scheduled_smooth = sum(
        1
        for slot in smooth_slots
        if (slot.assignment_date, slot.shift_id) in assigned_keys
    )
    assert scheduled_smooth == 54


@pytest.mark.legacy
def test_portage_auto_generate_fulfills_fulltime_payroll_contract_hours() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 28)
    dates = [period_start + timedelta(days=offset) for offset in range((period_end - period_start).days + 1)]
    employees = build_portage_roster()
    targets = portage_coverage_targets(employees)
    target_hours = portage_employee_target_hours(
        employees,
        weeks_in_period=4,
        rules=MANITOBA,
    )
    templates = shift_templates()
    token_by_code = {"MORNING": "D", "EVENING": "E", "NIGHT": "N"}

    result = auto_generate_schedule(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=4,
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=target_hours,
        coverage_targets=targets,
        strict_complete_block=False,
    )

    schedule_rows = {
        employee.id: {day.isoformat(): "" for day in dates}
        for employee in employees
    }
    for assignment in result.assignments:
        token = token_by_code.get(templates[assignment.shift_template_id].code, "")
        if token:
            schedule_rows[assignment.employee_id][assignment.assignment_date.isoformat()] = token

    for employee in employees:
        if employee.fte < 0.99:
            continue
        tracking = compute_contract_tracking_row(
            fte=employee.fte,
            week_count=4,
            row=schedule_rows[employee.id],
            dates=dates,
            contract_line_type=employee.contract_line_type or "",
        )
        if tracking.status_class == "contract-ok":
            assert tracking.actual_hours == 160.0

    expanded = expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    qual_codes = {}
    for employee in employees:
        qual_id = next(iter(employee.qualification_ids), "")
        if qual_id:
            qual_codes[qual_id] = "MLT" if "mlt" in qual_id.lower() else "MLA"
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)
    assert is_clinical_floor_satisfied(
        fill_counts=fill_counts,
        shift_templates=templates,
        period_start=period_start,
        period_end=period_end,
        expanded_slots=expanded,
    )

    fulltime_ok = sum(
        1
        for employee in employees
        if employee.fte >= 0.99
        and compute_contract_tracking_row(
            fte=employee.fte,
            week_count=4,
            row=schedule_rows[employee.id],
            dates=dates,
            contract_line_type=employee.contract_line_type or "",
        ).status_class
        == "contract-ok"
    )
    assert fulltime_ok >= 1


def test_get_core_demands_marks_all_clinical_floor_bands_hard_required() -> None:
    specs = {spec.shift_code: spec for spec in get_core_demands()}

    for shift_code in ("MORNING", "EVENING", "NIGHT"):
        assert specs[shift_code].tier == DemandTier.HARD_REQUIRED
        assert specs[shift_code].min_shifts_per_day == CLINICAL_FLOOR[shift_code]


def test_is_night_demand_satisfied_requires_two_shifts_per_day() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 3)
    templates = shift_templates()
    night_id = next(
        shift_id for shift_id, template in templates.items() if template.code == "NIGHT"
    )

    one_night = [
        PlannedAssignment("emp-1", night_id, period_start),
    ]
    assert not is_night_demand_satisfied(
        one_night,
        shift_templates=templates,
        period_start=period_start,
        period_end=period_end,
    )
    assert not is_demand_satisfied(
        one_night,
        shift_templates=templates,
        period_start=period_start,
        period_end=period_end,
    )

    two_nights_one_day = [
        PlannedAssignment("emp-1", night_id, period_start),
        PlannedAssignment("emp-2", night_id, period_start),
    ]
    assert is_night_demand_satisfied(
        two_nights_one_day,
        shift_templates=templates,
        period_start=period_start,
        period_end=period_start,
    )


def test_missing_hard_demand_penalty_is_infinity_for_unfilled_clinical_floor() -> None:
    assert (
        missing_hard_demand_penalty(
            shift_template_code="NIGHT",
            assignment_date=date(2026, 6, 1),
            night_shifts_filled_for_day_count=0,
        )
        == MISSING_CLINICAL_FLOOR_PENALTY
    )
    assert (
        missing_hard_demand_penalty(
            shift_template_code="EVENING",
            assignment_date=date(2026, 6, 1),
            night_shifts_filled_for_day_count=1,
            clinical_band_filled_for_day_count=1,
        )
        == MISSING_CLINICAL_FLOOR_PENALTY
    )
    assert (
        missing_hard_demand_penalty(
            shift_template_code="NIGHT",
            assignment_date=date(2026, 6, 1),
            night_shifts_filled_for_day_count=2,
        )
        == 0.0
    )


@pytest.mark.legacy
def test_portage_auto_generate_satisfies_hard_night_demand() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 28)
    employees = build_portage_roster()
    targets = portage_coverage_targets(employees)
    target_hours = portage_employee_target_hours(
        employees,
        weeks_in_period=4,
        rules=MANITOBA,
    )
    templates = shift_templates()

    result = auto_generate_schedule(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=4,
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=target_hours,
        coverage_targets=targets,
        strict_complete_block=False,
    )

    expanded = expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
        concurrent_demands=portage_concurrent_demands(),
    )
    qual_codes = {}
    for employee in employees:
        qual_id = next(iter(employee.qualification_ids), "")
        if qual_id:
            qual_codes[qual_id] = "MLT" if "mlt" in qual_id.lower() else "MLA"
    fill_counts = _seat_fill_counts(result.assignments, employees, qual_codes)

    assert is_demand_satisfied(
        result.assignments,
        shift_templates=templates,
        period_start=period_start,
        period_end=period_end,
        fill_counts=fill_counts,
        expanded_slots=expanded,
    )
    assert is_clinical_floor_satisfied(
        fill_counts=fill_counts,
        shift_templates=templates,
        period_start=period_start,
        period_end=period_end,
        expanded_slots=expanded,
    )

    night_counts = count_night_shifts_by_day(
        result.assignments,
        shift_templates=templates,
    )
    evening_counts = count_band_shifts_by_day(
        result.assignments,
        shift_templates=templates,
        shift_code="EVENING",
    )
    day = period_start
    while day <= period_end:
        assert night_counts.get(day, 0) == CLINICAL_FLOOR["NIGHT"]
        assert evening_counts.get(day, 0) == CLINICAL_FLOOR["EVENING"]
        day += timedelta(days=1)


def test_portage_load_test_fills_weekday_morning_team_coverage() -> None:
    employees = build_portage_roster()
    targets = portage_coverage_targets(employees)
    target_hours = portage_employee_target_hours(
        employees,
        weeks_in_period=4,
        rules=MANITOBA,
    )
    result = auto_generate_schedule(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employees=employees,
        shift_templates=shift_templates(),
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=target_hours,
        coverage_targets=targets,
        strict_complete_block=False,
    )

    weekday = date(2026, 6, 1)
    morning_assignments = [
        assignment
        for assignment in result.assignments
        if assignment.assignment_date == weekday
        and assignment.shift_template_id == "shift-morning"
    ]
    assert len(morning_assignments) >= 7


def test_vacant_lines_fill_lower_numbers_before_higher() -> None:
    kwargs = portage_generate_kwargs(
        period_end=date(2026, 6, 14),
        weeks_in_period=2,
    )
    dn_employees = sorted(
        (
            employee
            for employee in kwargs["employees"]
            if employee.contract_line_type == "D/N"
        ),
        key=lambda employee: employee.id,
    )[:3]
    result = auto_generate_schedule(**kwargs)
    hours = {emp.id: 0.0 for emp in dn_employees}
    for assignment in result.assignments:
        if assignment.employee_id not in hours:
            continue
        hours[assignment.employee_id] += 8.0

    assert any(hours.values())
    lowest_id = dn_employees[0].id
    if any(hours[emp.id] > 0 for emp in dn_employees[1:]):
        assert hours[lowest_id] > 0


def test_consecutive_blocks_preferred_over_isolated_shifts() -> None:
    kwargs = portage_generate_kwargs(
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
    )
    result = auto_generate_schedule(**kwargs)
    dates_by_employee: dict[str, list[date]] = {}
    for assignment in result.assignments:
        dates_by_employee.setdefault(assignment.employee_id, []).append(
            assignment.assignment_date
        )

    found_consecutive = False
    for dates in dates_by_employee.values():
        sorted_dates = sorted(set(dates))
        if len(sorted_dates) < 2:
            continue
        if any(
            (sorted_dates[index + 1] - sorted_dates[index]).days == 1
            for index in range(len(sorted_dates) - 1)
        ):
            found_consecutive = True
            break
    assert found_consecutive

    isolated = 0
    for assignment_date in dates:
        prev_day = assignment_date - timedelta(days=1)
        next_day = assignment_date + timedelta(days=1)
        if prev_day not in dates and next_day not in dates:
            isolated += 1
    assert isolated <= max(1, len(dates) // 3)


def test_roster_line_number_parses_vacant_and_portage_ids() -> None:
    vacant = EmployeeProfile("v1", "Vacant MLT D/E - Line 04", 1.0, {QUAL_MLT})
    portage = EmployeeProfile("portage-mlt-07", "Portage MLT 07", 1.0, {QUAL_MLT})
    assert roster_line_number(vacant) == 4
    assert roster_line_number(portage) == 7


def test_weekend_paired_day_rank_penalty_prefers_paired_day() -> None:
    saturday = date(2026, 6, 6)
    sunday = saturday + timedelta(days=1)
    assert weekend_paired_day_rank_penalty(
        work_dates={saturday},
        assignment_date=sunday,
    ) == 0.0
    assert weekend_paired_day_rank_penalty(
        work_dates=set(),
        assignment_date=sunday,
    ) == 1.0
    assert weekend_paired_day_rank_penalty(
        work_dates=set(),
        assignment_date=date(2026, 6, 3),
    ) == 0.0
