from datetime import date, timedelta

import pytest

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ScheduledShift
from lab_scheduler.engine.constraints import validate_contract_line_eligibility
from lab_scheduler.engine.demand import (
    PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
    build_assignment_rank_key,
    contract_band_weave_penalty,
    fatigue_guardrail_violation,
    horizontal_week_peer_balance_penalty,
    horizontal_workload_balance_key,
)
from lab_scheduler.models.employee import allowed_shift_codes_for_role_contract
from lab_scheduler.engine.constraints import coverage_priority_key
from lab_scheduler.scheduling.auto_generate import (
    EmployeeProfile,
    auto_generate_schedule,
    validate_assignment_change,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile as Profile
from lab_scheduler.scheduling.seniority_ranking import cba_rank_key
from lab_scheduler.simulation.hospital_stress import (
    QUAL_MLA,
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_coverage_targets,
    portage_employee_target_hours,
)
from portage_fixtures import portage_generate_kwargs


def test_contract_matrix_allows_dual_bands_per_line() -> None:
    assert allowed_shift_codes_for_role_contract("D/E", qual_code="MLT") == frozenset(
        {"MORNING", "EVENING"}
    )
    assert allowed_shift_codes_for_role_contract("D/N", qual_code="MLT") == frozenset(
        {"MORNING", "NIGHT"}
    )
    assert allowed_shift_codes_for_role_contract("D/E", qual_code="MLA") == frozenset(
        {"MORNING", "EVENING"}
    )
    assert allowed_shift_codes_for_role_contract("D/N", qual_code="MLA") == frozenset(
        {"MORNING", "NIGHT"}
    )


def test_contract_band_weave_prefers_alternate_band_after_monoculture_week() -> None:
    week_start = date(2026, 6, 8)
    records = [
        (week_start + timedelta(days=offset), "shift-evening")
        for offset in range(5)
    ]
    penalty_repeat = contract_band_weave_penalty(
        contract_line_type="D/E",
        assignment_records=records,
        assignment_date=week_start + timedelta(days=7),
        shift_template_code="EVENING",
    )
    penalty_switch = contract_band_weave_penalty(
        contract_line_type="D/E",
        assignment_records=records,
        assignment_date=week_start + timedelta(days=7),
        shift_template_code="MORNING",
    )
    assert penalty_repeat > penalty_switch


def test_mla_de_line_may_work_evening_shifts() -> None:
    assert validate_contract_line_eligibility("D/E", "EVENING", qual_code="MLA") is None
    assert validate_contract_line_eligibility("D/E", "MORNING", qual_code="MLA") is None
    assert validate_contract_line_eligibility("D/E", "NIGHT", qual_code="MLA") is not None


def test_fatigue_guardrail_blocks_seventh_consecutive_day() -> None:
    start = date(2026, 6, 1)
    work_dates = {start + timedelta(days=offset) for offset in range(6)}
    seventh = start + timedelta(days=6)
    violation = fatigue_guardrail_violation(work_dates, seventh)
    assert violation is not None
    assert str(PORTAGE_MAX_CONSECUTIVE_WORK_DAYS) in violation


def test_fatigue_guardrail_requires_two_day_rest_between_blocks() -> None:
    work_dates = {date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)}
    too_soon = date(2026, 6, 5)
    violation = fatigue_guardrail_violation(work_dates, too_soon)
    assert violation is not None
    assert "2-day rest" in violation

    allowed = date(2026, 6, 6)
    assert fatigue_guardrail_violation(work_dates, allowed) is None


def test_validate_assignment_change_blocks_seventh_consecutive_day() -> None:
    employee = Profile(
        "emp-mlt-01",
        "MLT 01 (1.0 D/E)",
        1.0,
        {QUAL_MLT},
        contract_line_type="D/E",
    )
    start = date(2026, 6, 1)
    scheduled_shifts = [
        ScheduledShift(
            employee_id=employee.id,
            employee_name=employee.full_name,
            assignment_date=start + timedelta(days=offset),
            shift_template_id="shift-evening",
        )
        for offset in range(6)
    ]
    violation = validate_assignment_change(
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        employee=employee,
        all_assignments=scheduled_shifts,
        shift_templates=shift_templates(),
        shift_required_qualifications=shift_required_qualifications(),
        assignment_date=start + timedelta(days=6),
        new_shift_template_id="shift-evening",
        enforce_fte_target=False,
    )
    assert violation is not None
    assert "consecutive" in violation.lower() or "rest" in violation.lower()


@pytest.mark.legacy
def test_auto_generate_respects_six_day_fatigue_cap() -> None:
    kwargs = portage_generate_kwargs()
    result = auto_generate_schedule(**kwargs)
    dates_by_employee: dict[str, list[date]] = {}
    for assignment in result.assignments:
        dates_by_employee.setdefault(assignment.employee_id, []).append(
            assignment.assignment_date
        )

    for employee_id, dates in dates_by_employee.items():
        sorted_dates = sorted(set(dates))
        streak = 1
        max_streak = 1
        for index in range(1, len(sorted_dates)):
            if sorted_dates[index] == sorted_dates[index - 1] + timedelta(days=1):
                streak += 1
                max_streak = max(max_streak, streak)
            else:
                streak = 1
        assert max_streak <= PORTAGE_MAX_CONSECUTIVE_WORK_DAYS


def test_horizontal_week_peer_penalty_deprioritizes_loaded_line_when_peer_starving() -> None:
    week_start = date(2026, 6, 1)
    assignment_date = date(2026, 6, 4)
    loaded = Profile("emp-high", "Vacant MLA D/E - Line 01", 1.0, {QUAL_MLA}, contract_line_type="D/E")
    starving = Profile("emp-low", "Vacant MLA D/E - Line 08", 0.8, {QUAL_MLA}, contract_line_type="D/E")
    loaded_penalty = horizontal_week_peer_balance_penalty(
        loaded,
        assignment_date=assignment_date,
        week_hours={week_start: 16.0},
        employees=[loaded, starving],
        employee_total_hours={"emp-high": 80.0, "emp-low": 8.0},
        employee_target_hours={"emp-high": 160.0, "emp-low": 128.0},
        qual_codes={QUAL_MLA: "MLA"},
    )
    idle_penalty = horizontal_week_peer_balance_penalty(
        starving,
        assignment_date=assignment_date,
        week_hours={week_start: 0.0},
        employees=[loaded, starving],
        employee_total_hours={"emp-high": 80.0, "emp-low": 8.0},
        employee_target_hours={"emp-high": 160.0, "emp-low": 128.0},
        qual_codes={QUAL_MLA: "MLA"},
    )
    assert loaded_penalty > idle_penalty


def test_horizontal_workload_balance_prefers_underloaded_line() -> None:
    starving = horizontal_workload_balance_key(0.0, 160.0)
    loaded = horizontal_workload_balance_key(136.0, 160.0)
    assert starving < loaded


def test_build_assignment_rank_key_prioritizes_hour_deficit_over_overload() -> None:
    assignment_date = date(2026, 6, 3)
    week_start = date(2026, 6, 1)
    starving = Profile("emp-low", "Vacant MLA D/E - Line 08", 0.8, {QUAL_MLA}, contract_line_type="D/E")
    loaded = Profile("emp-high", "Vacant MLA D/E - Line 01", 1.0, {QUAL_MLA}, contract_line_type="D/E")

    starving_key = build_assignment_rank_key(
        profile=starving,
        work_dates=set(),
        assignment_records=[],
        week_hours={week_start: 0.0},
        total_hours=0.0,
        assignment_date=assignment_date,
        shift_id="shift-evening",
        shift_hours=8.0,
        shift_template_code="EVENING",
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        employees=[starving, loaded],
        employee_total_hours={"emp-low": 0.0, "emp-high": 136.0},
        employee_target_hours={"emp-low": 128.0, "emp-high": 160.0},
        qual_codes={QUAL_MLA: "MLA"},
        prioritize_coverage=True,
        period_target_hours={"emp-low": 128.0, "emp-high": 160.0},
        coverage_priority_key_fn=coverage_priority_key,
        cba_rank_key_fn=cba_rank_key,
    )
    loaded_key = build_assignment_rank_key(
        profile=loaded,
        work_dates={date(2026, 6, 2)},
        assignment_records=[(date(2026, 6, 2), "shift-evening")],
        week_hours={week_start: 8.0},
        total_hours=136.0,
        assignment_date=assignment_date,
        shift_id="shift-evening",
        shift_hours=8.0,
        shift_template_code="EVENING",
        rules=MANITOBA,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        employees=[starving, loaded],
        employee_total_hours={"emp-low": 0.0, "emp-high": 136.0},
        employee_target_hours={"emp-low": 128.0, "emp-high": 160.0},
        qual_codes={QUAL_MLA: "MLA"},
        prioritize_coverage=True,
        period_target_hours={"emp-low": 128.0, "emp-high": 160.0},
        coverage_priority_key_fn=coverage_priority_key,
        cba_rank_key_fn=cba_rank_key,
    )
    assert starving_key < loaded_key


def test_portage_schedule_avoids_extreme_hour_imbalance() -> None:
    kwargs = portage_generate_kwargs()
    mla_employees = [
        employee for employee in kwargs["employees"] if "portage-mla" in employee.id
    ]
    result = auto_generate_schedule(**kwargs)
    hours_by_employee: dict[str, float] = {employee.id: 0.0 for employee in mla_employees}
    templates = kwargs["shift_templates"]
    for assignment in result.assignments:
        if assignment.employee_id not in hours_by_employee:
            continue
        template = templates[assignment.shift_template_id]
        hours_by_employee[assignment.employee_id] += template.duration_minutes / 60.0

    worked = [hours for hours in hours_by_employee.values() if hours > 0.01]
    assert len(worked) >= 2
    assert max(worked) - min(worked) < 120.0
    assert min(worked) > 0.0
