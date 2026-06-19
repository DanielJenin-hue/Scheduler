
import pytest

pytestmark = pytest.mark.legacy

from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

import pytest

from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.profiles import EmployeeProfile

ortools = pytest.importorskip("ortools")

from lab_scheduler.scheduling.fairness_thresholds import (  # noqa: E402
    DEFAULT_FAIRNESS_THRESHOLDS,
    FairnessThresholds,
    WEIGHT_EVENING_CLUSTER,
    WEIGHT_POST_NIGHT_RECOVERY,
)
from lab_scheduler.solver.cpsat_fill import (  # noqa: E402
    CONTRACT_COVERAGE_WEIGHT_MULTIPLIER,
    DAILY_EVENING_CAP,
    DAILY_NIGHT_CAP,
    WEIGHT_ALT_SHIFT_EQUITY,
    WEIGHT_ALT_SHIFT_UNFAIRNESS,
    WEIGHT_COVERAGE_SHORTFALL,
    WEIGHT_PT_ALT_SHIFT_CEILING_SLACK,
    WEIGHT_DEFICIT_VARIANCE,
    WEIGHT_HOUR_DEFICIT,
    WEIGHT_HOUR_DEFICIT_FULLTIME,
    WEIGHT_MAX_PREFERENCE,
    WEIGHT_MIN_CONTRACT_COVERAGE,
    WEIGHT_N_TO_D_FATIGUE,
    WEIGHT_PT_CATALOG_SURPLUS_EXCESS_MID,
    WEIGHT_PT_CATALOG_SURPLUS_EXCESS_SEVERE,
    WEIGHT_PT_CATALOG_SURPLUS_GRACE,
    WEIGHT_PT_PAYROLL_SURPLUS,
    WEIGHT_WEEKDAY_SURPLUS_SMOOTH,
    WEEKEND_DAY_CAP,
    alternate_shift_rows_from_equity_metrics,
    compute_employee_alternate_shift_share,
    format_shift_equity_metrics_summary,
    _parttime_max_allowed_alt_shifts,
    _parttime_min_allowed_alt_shifts,
    _parttime_allowed_alt_band,
    shift_code_to_band,
    solve_vacant_unassigned_slots,
)


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


def _solve_without_band_caps(**kwargs: object):
    return solve_vacant_unassigned_slots(portage_daily_band_caps=False, **kwargs)


def _band_code(shift_template_id: str) -> str:
    return _templates()[shift_template_id].code


def _pool_band_count(assignments, *, band_code: str, assignment_date: date) -> int:
    return sum(
        1
        for assignment in assignments
        if assignment.assignment_date == assignment_date
        and _band_code(assignment.shift_template_id) == band_code
    )


def _alternate_shift_count(assignments, *, employee_id: str) -> int:
    total = 0
    for assignment in assignments:
        if assignment.employee_id != employee_id:
            continue
        band = shift_code_to_band(_band_code(assignment.shift_template_id))
        if band in ("E", "N"):
            total += 1
    return total


def test_cpsat_enforces_one_shift_per_day_and_e_to_d_rest() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=2)
    target_hours = {"vacant-01": 24.0}

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=10.0,
    )

    by_day: dict[date, str] = {}
    for assignment in result.assignments:
        template = _templates()[assignment.shift_template_id]
        assert assignment.employee_id == "vacant-01"
        assert assignment.assignment_date not in by_day
        by_day[assignment.assignment_date] = template.code

    for day, band in list(by_day.items()):
        next_day = day + timedelta(days=1)
        if next_day in by_day and band == "EVENING":
            assert by_day[next_day] != "MORNING"


def test_cpsat_ignores_vacant_upstream_fixed_cells_and_opens_full_grid() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=1)
    fixed = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-evening",
            assignment_date=period_start,
        )
    ]

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=fixed,
        employee_target_hours={"vacant-01": 16.0},
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=10.0,
    )

    assert result.fillable_slot_count == 2
    assert len(result.assignments) == 2


def test_cpsat_forbids_day_before_night_next_calendar_day() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/N - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=2)
    fixed = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-morning",
            assignment_date=period_start,
        )
    ]

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=fixed,
        employee_target_hours={"vacant-01": 24.0},
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=10.0,
    )

    assert all(
        not (
            assignment.employee_id == "vacant-01"
            and assignment.assignment_date == period_start
            and _templates()[assignment.shift_template_id].code == "MORNING"
            and any(
                other.employee_id == "vacant-01"
                and other.assignment_date == period_start + timedelta(days=1)
                and _templates()[other.shift_template_id].code == "NIGHT"
                for other in result.assignments
            )
        )
        for assignment in result.assignments
    )


def test_cpsat_avoids_night_before_day_when_alternative_exists() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/N - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        ),
        EmployeeProfile(
            "vacant-02",
            "Vacant MLT D/N - Line 02",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        ),
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=1)
    fixed = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-night",
            assignment_date=period_start,
        )
    ]
    target_hours = {"vacant-01": 16.0, "vacant-02": 16.0}

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=fixed,
        employee_target_hours=target_hours,
        qual_codes={"vacant-01": "MLT", "vacant-02": "MLT"},
        time_limit_seconds=10.0,
    )

    assert result.n_to_d_fatigue_total == 0
    hours_by_employee = {"vacant-01": 8.0, "vacant-02": 0.0}
    for assignment in result.assignments:
        hours_by_employee[assignment.employee_id] += 8.0
    assert hours_by_employee["vacant-01"] >= target_hours["vacant-01"]
    assert hours_by_employee["vacant-02"] >= target_hours["vacant-02"]


def test_n_to_d_fatigue_weight_is_last_resort_vs_hour_deficit() -> None:
    assert WEIGHT_N_TO_D_FATIGUE < WEIGHT_HOUR_DEFICIT
    assert WEIGHT_HOUR_DEFICIT < WEIGHT_HOUR_DEFICIT_FULLTIME
    assert WEIGHT_HOUR_DEFICIT_FULLTIME == 500_000
    assert WEIGHT_DEFICIT_VARIANCE < WEIGHT_HOUR_DEFICIT
    assert WEIGHT_ALT_SHIFT_EQUITY < WEIGHT_HOUR_DEFICIT
    assert WEIGHT_ALT_SHIFT_EQUITY < WEIGHT_DEFICIT_VARIANCE
    assert WEIGHT_PT_ALT_SHIFT_CEILING_SLACK < WEIGHT_ALT_SHIFT_EQUITY
    assert WEIGHT_PT_ALT_SHIFT_CEILING_SLACK < WEIGHT_ALT_SHIFT_UNFAIRNESS
    assert WEIGHT_ALT_SHIFT_UNFAIRNESS > WEIGHT_ALT_SHIFT_EQUITY


def test_contract_coverage_weights_dominate_preference_weights() -> None:
    assert WEIGHT_MAX_PREFERENCE == 600
    assert WEIGHT_MIN_CONTRACT_COVERAGE == WEIGHT_MAX_PREFERENCE * CONTRACT_COVERAGE_WEIGHT_MULTIPLIER
    preference_weights = (
        WEIGHT_ALT_SHIFT_EQUITY,
        WEIGHT_ALT_SHIFT_UNFAIRNESS,
        WEIGHT_PT_ALT_SHIFT_CEILING_SLACK,
        WEIGHT_DEFICIT_VARIANCE,
        WEIGHT_WEEKDAY_SURPLUS_SMOOTH,
        WEIGHT_N_TO_D_FATIGUE,
        WEIGHT_EVENING_CLUSTER,
        WEIGHT_POST_NIGHT_RECOVERY,
    )
    contract_weights = (
        WEIGHT_HOUR_DEFICIT,
        WEIGHT_HOUR_DEFICIT_FULLTIME,
        WEIGHT_PT_PAYROLL_SURPLUS,
        WEIGHT_PT_CATALOG_SURPLUS_GRACE,
        WEIGHT_COVERAGE_SHORTFALL,
    )
    for preference in preference_weights:
        assert preference <= WEIGHT_MAX_PREFERENCE
    for contract in contract_weights:
        assert contract >= WEIGHT_MIN_CONTRACT_COVERAGE


def test_pt_catalog_surplus_weight_ordering() -> None:
    assert WEIGHT_PT_PAYROLL_SURPLUS > WEIGHT_HOUR_DEFICIT_FULLTIME
    assert WEIGHT_PT_CATALOG_SURPLUS_GRACE == 20_000
    assert WEIGHT_PT_CATALOG_SURPLUS_GRACE < WEIGHT_PT_PAYROLL_SURPLUS
    assert WEIGHT_HOUR_DEFICIT_FULLTIME < WEIGHT_PT_CATALOG_SURPLUS_EXCESS_MID
    assert WEIGHT_PT_CATALOG_SURPLUS_EXCESS_MID < WEIGHT_PT_CATALOG_SURPLUS_EXCESS_SEVERE


def test_parttime_max_allowed_alt_shifts_uses_twenty_percent_target() -> None:
    assert _parttime_max_allowed_alt_shifts(160.0) == 4
    assert _parttime_max_allowed_alt_shifts(128.0) == 3
    assert _parttime_max_allowed_alt_shifts(64.0) == 2
    assert _parttime_max_allowed_alt_shifts(312.0) == 8


def test_parttime_min_allowed_alt_shifts_uses_twenty_percent_target() -> None:
    assert _parttime_min_allowed_alt_shifts(160.0) == 4
    assert _parttime_min_allowed_alt_shifts(128.0) == 3
    assert _parttime_min_allowed_alt_shifts(64.0) == 2
    assert _parttime_min_allowed_alt_shifts(312.0) == 8


def test_parttime_allowed_alt_band_is_exact_twenty_percent() -> None:
    assert _parttime_allowed_alt_band(64.0) == (2, 2)
    assert _parttime_allowed_alt_band(32.0) == (1, 1)
    assert _parttime_allowed_alt_band(160.0) == (4, 4)


def test_parttime_alt_shift_density_band_limits_and_floors_smallest_lines() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "vacant-02",
            "Vacant MLT D/E - Line 02",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "vacant-08",
            "Vacant MLT D/E - Line 08",
            0.5,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "vacant-09",
            "Vacant MLT D/E - Line 09",
            0.2,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
    ]
    target_hours = {
        "vacant-01": 320.0,
        "vacant-02": 320.0,
        "vacant-08": 160.0,
        "vacant-09": 64.0,
    }
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={
            "vacant-01": "MLT",
            "vacant-02": "MLT",
            "vacant-08": "MLT",
            "vacant-09": "MLT",
        },
        time_limit_seconds=30.0,
    )

    pt_limits = {
        employee_id: (
            _parttime_min_allowed_alt_shifts(target_hours[employee_id]),
            _parttime_max_allowed_alt_shifts(target_hours[employee_id]),
        )
        for employee_id in ("vacant-08", "vacant-09")
    }
    for employee_id, (min_allowed, max_allowed) in pt_limits.items():
        alt_count = _alternate_shift_count(result.assignments, employee_id=employee_id)
        assert alt_count <= max_allowed + 1
        if min_allowed > 0:
            assert alt_count >= min_allowed - 1
    assert result.pt_alt_band_slack_total <= 3


def _evening_shift_count(assignments, *, employee_id: str) -> int:
    return sum(
        1
        for assignment in assignments
        if assignment.employee_id == employee_id
        and _band_code(assignment.shift_template_id) == "EVENING"
    )


def _max_consecutive_evening_run(assignments, *, employee_id: str) -> int:
    evening_dates = sorted(
        assignment.assignment_date
        for assignment in assignments
        if assignment.employee_id == employee_id
        and _band_code(assignment.shift_template_id) == "EVENING"
    )
    if not evening_dates:
        return 0
    max_run = 1
    current_run = 1
    for index in range(1, len(evening_dates)):
        if (evening_dates[index] - evening_dates[index - 1]).days == 1:
            current_run += 1
            max_run = max(max_run, current_run)
        else:
            current_run = 1
    return max_run


def test_fulltime_alt_shift_soft_range_stays_within_eight_to_twelve() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"vacant-01": 320.0},
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=20.0,
    )

    alt_count = _alternate_shift_count(result.assignments, employee_id="vacant-01")
    assert 8 <= alt_count <= 12
    assert result.alt_shift_unfairness_total == 0


def test_fulltime_lines_carry_alternate_burden_vs_part_time_peer() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "vacant-02",
            "Vacant MLT D/E - Line 02",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "vacant-09",
            "Vacant MLT D/E - Line 09",
            0.5,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)
    target_hours = {
        "vacant-01": 320.0,
        "vacant-02": 320.0,
        "vacant-09": 160.0,
    }

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={
            "vacant-01": "MLT",
            "vacant-02": "MLT",
            "vacant-09": "MLT",
        },
        time_limit_seconds=25.0,
    )

    ft_alt = [
        _alternate_shift_count(result.assignments, employee_id=employee_id)
        for employee_id in ("vacant-01", "vacant-02")
    ]
    pt_alt = _alternate_shift_count(result.assignments, employee_id="vacant-09")
    ft_avg = sum(ft_alt) / len(ft_alt)
    assert min(ft_alt) >= 8
    assert max(ft_alt) <= 12
    assert pt_alt <= _parttime_max_allowed_alt_shifts(160.0) + 1
    assert pt_alt <= ft_avg + 2


def test_master_rotation_ft_de_lines_average_eight_evenings() -> None:
    from lab_scheduler.engine.constraints import portage_employee_target_hours
    from lab_scheduler.simulation.portage_blueprint import build_portage_blueprint_roster

    employees = build_portage_blueprint_roster()
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)
    target_hours = portage_employee_target_hours(
        employees,
        weeks_in_period=8,
        rules=MANITOBA,
    )

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        time_limit_seconds=120.0,
    )

    assert result.status in {"OPTIMAL", "FEASIBLE"}

    ft_de_employees = [
        employee
        for employee in employees
        if employee.contract_line_type == "D/E"
        and float(target_hours[employee.id]) >= 312.0
    ]
    pt_de_line_names = {
        "Vacant MLA D/E - Line 06",
        "Vacant MLA D/E - Line 07",
        "Vacant MLA D/E - Line 08",
    }
    pt_de_employees = [
        employee for employee in employees if employee.full_name in pt_de_line_names
    ]

    ft_evening_counts = [
        _evening_shift_count(result.assignments, employee_id=employee.id)
        for employee in ft_de_employees
    ]
    pt_evening_counts = [
        _evening_shift_count(result.assignments, employee_id=employee.id)
        for employee in pt_de_employees
    ]
    ft_avg_evenings = sum(ft_evening_counts) / len(ft_evening_counts)

    assert 7 <= round(ft_avg_evenings) <= 9
    assert all(8 <= count <= 12 for count in ft_evening_counts)
    assert max(pt_evening_counts) <= ft_avg_evenings + 2

    ft_max_evening_runs = [
        _max_consecutive_evening_run(result.assignments, employee_id=employee.id)
        for employee in ft_de_employees
    ]
    pt_max_evening_runs = [
        _max_consecutive_evening_run(result.assignments, employee_id=employee.id)
        for employee in pt_de_employees
    ]
    assert max(pt_max_evening_runs) <= max(ft_max_evening_runs) + 1
    assert result.alt_shift_unfairness_total <= 24


def test_identical_vacant_lines_balance_alternate_shifts_de_pool() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "vacant-02",
            "Vacant MLT D/E - Line 02",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=6)
    target_hours = {"vacant-01": 40.0, "vacant-02": 40.0}

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={"vacant-01": "MLT", "vacant-02": "MLT"},
        time_limit_seconds=15.0,
    )

    alt_counts = [
        _alternate_shift_count(result.assignments, employee_id=employee_id)
        for employee_id in ("vacant-01", "vacant-02")
    ]
    assert max(alt_counts) - min(alt_counts) <= 1
    assert result.alt_shift_spread_total <= 1


def test_identical_vacant_lines_balance_alternate_shifts_dn_pool() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/N - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        ),
        EmployeeProfile(
            "vacant-03",
            "Vacant MLT D/N - Line 03",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        ),
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=6)
    target_hours = {"vacant-01": 40.0, "vacant-03": 40.0}

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={"vacant-01": "MLT", "vacant-03": "MLT"},
        time_limit_seconds=15.0,
    )

    alt_counts = [
        _alternate_shift_count(result.assignments, employee_id=employee_id)
        for employee_id in ("vacant-01", "vacant-03")
    ]
    assert max(alt_counts) - min(alt_counts) <= 1
    assert result.alt_shift_spread_total <= 1


def test_identical_vacant_lines_balance_hour_deficits() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "vacant-02",
            "Vacant MLT D/E - Line 02",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=1)
    target_hours = {"vacant-01": 16.0, "vacant-02": 16.0}

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={"vacant-01": "MLT", "vacant-02": "MLT"},
        time_limit_seconds=10.0,
    )

    hours_by_employee = {"vacant-01": 0.0, "vacant-02": 0.0}
    for assignment in result.assignments:
        hours_by_employee[assignment.employee_id] += 8.0

    deficits = [
        max(0.0, target_hours[employee_id] - hours_by_employee[employee_id])
        for employee_id in hours_by_employee
    ]
    assert max(deficits) - min(deficits) <= 8.0
    assert result.deficit_variance_total <= 8


def test_cpsat_leaves_open_days_off_when_contract_is_met() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=2)

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"vacant-01": 24.0},
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=10.0,
    )

    assert result.unfilled_escalated_total == 0
    assert len(result.assignments) == 3


def test_cpsat_forbids_n_to_d_after_fixed_night() -> None:
    """N→D is a hard constraint; Day on t+1 is forbidden after a fixed Night on day t."""
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/N - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=1)
    fixed = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-night",
            assignment_date=period_start,
        )
    ]

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=fixed,
        employee_target_hours={"vacant-01": 16.0},
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=10.0,
    )

    day_two = [
        assignment
        for assignment in result.assignments
        if assignment.employee_id == "vacant-01"
        and assignment.assignment_date == period_end
    ]
    assert all(
        _templates()[assignment.shift_template_id].code != "MORNING"
        for assignment in day_two
    )


def test_cpsat_d_e_contract_line_blocks_night() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=6)

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"vacant-01": 320.0},
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=10.0,
    )

    for assignment in result.assignments:
        assert assignment.shift_template_id != "shift-night"


def _vacant_de_lines(count: int) -> list[EmployeeProfile]:
    return [
        EmployeeProfile(
            f"vacant-{index:02d}",
            f"Vacant MLT D/E - Line {index:02d}",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
        for index in range(1, count + 1)
    ]


def _period_dates(period_start: date, period_end: date) -> list[date]:
    days: list[date] = []
    cursor = period_start
    while cursor <= period_end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _assert_portage_band_caps(
    assignments: tuple[PlannedAssignment, ...] | list[PlannedAssignment],
    period_dates: list[date],
) -> None:
    for assignment_date in period_dates:
        assert (
            _pool_band_count(
                assignments, band_code="EVENING", assignment_date=assignment_date
            )
            == DAILY_EVENING_CAP
        )
        assert (
            _pool_band_count(
                assignments, band_code="NIGHT", assignment_date=assignment_date
            )
            == DAILY_NIGHT_CAP
        )
        if assignment_date.weekday() >= 5:
            assert (
                _pool_band_count(
                    assignments, band_code="MORNING", assignment_date=assignment_date
                )
                == WEEKEND_DAY_CAP
            )


def _vacant_dn_lines(count: int) -> list[EmployeeProfile]:
    return [
        EmployeeProfile(
            f"vacant-dn-{index:02d}",
            f"Vacant MLT D/N - Line {index:02d}",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        )
        for index in range(1, count + 1)
    ]


def test_cpsat_portage_daily_evening_cap_is_exactly_two() -> None:
    employees = _mixed_de_dn_roster()
    period_start = date(2026, 6, 1)  # Monday
    period_end = period_start
    target_hours = {employee.id: 8.0 for employee in employees}

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={
            employee.id: ("MLT" if "MLT" in employee.full_name else "MLA")
            for employee in employees
        },
        time_limit_seconds=30.0,
    )

    assert (
        _pool_band_count(result.assignments, band_code="EVENING", assignment_date=period_start)
        == DAILY_EVENING_CAP
    )
    assert len(result.assignments) == len(employees)


def test_cpsat_portage_daily_night_cap_is_exactly_two() -> None:
    employees = [
        EmployeeProfile(
            f"vacant-mlt-dn-{index:02d}",
            f"Vacant MLT D/N - Line {index:02d}",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        )
        for index in range(1, 5)
    ] + [
        EmployeeProfile(
            f"vacant-mla-dn-{index:02d}",
            f"Vacant MLA D/N - Line {index:02d}",
            1.0,
            {"qual-mla"},
            contract_line_type="D/N",
        )
        for index in range(1, 5)
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start
    target_hours = {employee.id: 8.0 for employee in employees}

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={
            employee.id: ("MLT" if "MLT" in employee.full_name else "MLA")
            for employee in employees
        },
        time_limit_seconds=30.0,
    )

    assert (
        _pool_band_count(result.assignments, band_code="NIGHT", assignment_date=period_start)
        == DAILY_NIGHT_CAP
    )
    assert len(result.assignments) == len(employees)


def _pool_band_qual_count(
    assignments,
    *,
    band_code: str,
    qual_code: str,
    assignment_date: date,
    employees: list[EmployeeProfile],
) -> int:
    employee_qual = {
        employee.id: ("MLT" if "mlt" in employee.id.lower() or "MLT" in employee.full_name else "MLA")
        for employee in employees
    }
    for employee in employees:
        if employee.id.startswith("portage-"):
            employee_qual[employee.id] = "MLT" if "mlt" in employee.id else "MLA"
        elif employee.id.startswith("vacant"):
            if "MLT" in employee.full_name:
                employee_qual[employee.id] = "MLT"
            elif "MLA" in employee.full_name:
                employee_qual[employee.id] = "MLA"

    return sum(
        1
        for assignment in assignments
        if assignment.assignment_date == assignment_date
        and _band_code(assignment.shift_template_id) == band_code
        and employee_qual.get(assignment.employee_id) == qual_code
    )


def _mixed_de_dn_roster() -> list[EmployeeProfile]:
    lines: list[EmployeeProfile] = []
    for index in range(1, 5):
        lines.append(
            EmployeeProfile(
                f"vacant-mlt-de-{index:02d}",
                f"Vacant MLT D/E - Line {index:02d}",
                1.0,
                {"qual-mlt"},
                contract_line_type="D/E",
            )
        )
        lines.append(
            EmployeeProfile(
                f"vacant-mla-de-{index:02d}",
                f"Vacant MLA D/E - Line {index:02d}",
                1.0,
                {"qual-mla"},
                contract_line_type="D/E",
            )
        )
    for index in range(1, 3):
        lines.append(
            EmployeeProfile(
                f"vacant-mlt-dn-{index:02d}",
                f"Vacant MLT D/N - Line {index:02d}",
                1.0,
                {"qual-mlt"},
                contract_line_type="D/N",
            )
        )
        lines.append(
            EmployeeProfile(
                f"vacant-mla-dn-{index:02d}",
                f"Vacant MLA D/N - Line {index:02d}",
                1.0,
                {"qual-mla"},
                contract_line_type="D/N",
            )
        )
    return lines


def test_cpsat_portage_evening_and_night_require_one_mlt_and_one_mla() -> None:
    employees = _mixed_de_dn_roster()
    period_start = date(2026, 6, 1)
    period_end = period_start
    target_hours = {employee.id: 8.0 for employee in employees}

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={
            employee.id: ("MLT" if "MLT" in employee.full_name else "MLA")
            for employee in employees
        },
        time_limit_seconds=30.0,
    )

    assert (
        _pool_band_qual_count(
            result.assignments,
            band_code="EVENING",
            qual_code="MLT",
            assignment_date=period_start,
            employees=employees,
        )
        == 1
    )
    assert (
        _pool_band_qual_count(
            result.assignments,
            band_code="EVENING",
            qual_code="MLA",
            assignment_date=period_start,
            employees=employees,
        )
        == 1
    )
    assert (
        _pool_band_qual_count(
            result.assignments,
            band_code="NIGHT",
            qual_code="MLT",
            assignment_date=period_start,
            employees=employees,
        )
        == 1
    )
    assert (
        _pool_band_qual_count(
            result.assignments,
            band_code="NIGHT",
            qual_code="MLA",
            assignment_date=period_start,
            employees=employees,
        )
        == 1
    )


def test_cpsat_portage_weekend_day_cap_is_exactly_two() -> None:
    employees = [
        EmployeeProfile(
            f"vacant-mlt-de-{index:02d}",
            f"Vacant MLT D/E - Line {index:02d}",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
        for index in range(1, 6)
    ] + [
        EmployeeProfile(
            f"vacant-mla-de-{index:02d}",
            f"Vacant MLA D/E - Line {index:02d}",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        )
        for index in range(1, 6)
    ]
    period_start = date(2026, 6, 6)  # Saturday
    period_end = date(2026, 6, 7)  # Sunday
    target_hours = {employee.id: 16.0 for employee in employees}

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={
            employee.id: ("MLT" if "MLT" in employee.full_name else "MLA")
            for employee in employees
        },
        time_limit_seconds=30.0,
    )

    for assignment_date in _period_dates(period_start, period_end):
        assert (
            _pool_band_count(
                result.assignments, band_code="MORNING", assignment_date=assignment_date
            )
            == WEEKEND_DAY_CAP
        )
        assert (
            _pool_band_qual_count(
                result.assignments,
                band_code="MORNING",
                qual_code="MLT",
                assignment_date=assignment_date,
                employees=employees,
            )
            == 1
        )
        assert (
            _pool_band_qual_count(
                result.assignments,
                band_code="MORNING",
                qual_code="MLA",
                assignment_date=assignment_date,
                employees=employees,
            )
            == 1
        )
    assert result.unfilled_escalated_total == 0


def test_cpsat_portage_macro_band_caps_wired_in_solver() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "lab_scheduler"
        / "solver"
        / "cpsat_fill.py"
    ).read_text(encoding="utf-8")
    assert "def _add_portage_daily_band_caps" in source
    assert "def _add_clinical_floor_qual_caps" in source
    assert "def _add_portage_consecutive_work_limit" in source
    assert "def _add_portage_consecutive_night_limit" in source
    assert "def _add_portage_weekend_mirror_rule" in source
    assert "def _add_portage_weekend_active_caps" in source
    assert "def compute_shift_equity_metrics" in source
    assert "def _add_alt_shift_equity_objective" in source
    assert "def _add_fulltime_alt_shift_range_objective" in source
    assert "def _add_parttime_alt_shift_band_objective" in source
    assert "def _add_weekday_day_smoothing_objective" in source
    assert "def _add_evening_cluster_objective" in source
    assert "def _add_post_night_recovery_objective" in source
    assert "evening_cluster_slack_total" in source
    assert "fairness_penalty_total" in source
    assert "num_search_workers" in source
    assert "WEIGHT_ALT_SHIFT_EQUITY" in source
    assert "WEIGHT_ALT_SHIFT_UNFAIRNESS" in source
    assert "WEIGHT_PT_ALT_SHIFT_CEILING_SLACK" in source
    assert "PARTTIME_ALT_SHIFT_DENSITY_CEILING" in source
    assert "PARTTIME_ALT_SHIFT_DENSITY_FLOOR" in source
    assert "WEIGHT_WEEKDAY_SURPLUS_SMOOTH" in source
    assert "portage_daily_band_caps: bool = True" in source


def test_cpsat_solver_has_no_daily_baseline_maximum_constraints() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "lab_scheduler"
        / "solver"
        / "cpsat_fill.py"
    ).read_text(encoding="utf-8")
    assert "shift_target_for_portage_date" not in source
    assert "assigned_expr <= target" not in source
    assert "AddLessOrEqual(assigned_expr, target)" not in source


def test_cpsat_fills_contract_hours_without_baseline_ceiling() -> None:
    employees = _vacant_de_lines(8)
    period_start = date(2026, 6, 1)  # Monday
    period_end = period_start + timedelta(days=3)  # Thursday
    target_hours = {employee.id: 32.0 for employee in employees}

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={employee.id: "MLT" for employee in employees},
        time_limit_seconds=30.0,
    )

    for employee in employees:
        worked_hours = sum(
            8.0
            for assignment in result.assignments
            if assignment.employee_id == employee.id
        )
        assert worked_hours >= target_hours[employee.id]
    assert result.coverage_shortfall_total == 0
    assert result.weekday_surplus_spread_total == 0


def test_cpsat_portage_blueprint_roster_respects_daily_band_caps() -> None:
    from lab_scheduler.engine.constraints import portage_employee_target_hours
    from lab_scheduler.simulation.portage_blueprint import build_portage_blueprint_roster

    employees = build_portage_blueprint_roster()
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=6)
    period_dates = _period_dates(period_start, period_end)
    target_hours = portage_employee_target_hours(
        employees,
        weeks_in_period=1,
        rules=MANITOBA,
    )

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=1,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        time_limit_seconds=60.0,
    )

    _assert_portage_band_caps(result.assignments, period_dates)
    weekday_d_counts = [
        _pool_band_count(
            result.assignments, band_code="MORNING", assignment_date=assignment_date
        )
        for assignment_date in period_dates
        if assignment_date.weekday() < 5
    ]
    if len(weekday_d_counts) >= 2:
        assert max(weekday_d_counts) - min(weekday_d_counts) <= 6
        assert result.weekday_surplus_spread_total == max(weekday_d_counts) - min(
            weekday_d_counts
        )


def test_cpsat_single_vacant_mla_de_line_meets_320h_over_8_weeks() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLA D/E - Line 01",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"vacant-01": 320.0},
        qual_codes={"vacant-01": "MLA"},
        time_limit_seconds=30.0,
    )

    worked_hours = sum(
        8.0 for assignment in result.assignments if assignment.employee_id == "vacant-01"
    )
    assert worked_hours == 320.0
    assert len(result.assignments) == 40


def test_cpsat_compliance_first_caps_fulltime_vacant_at_payroll_not_catalog() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLA D/E - Line 01",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"vacant-01": 320.0},
        catalog_target_hours={"vacant-01": 328.0},
        qual_codes={"vacant-01": "MLA"},
        time_limit_seconds=30.0,
        portage_daily_band_caps=False,
        compliance_first=True,
    )

    worked_hours = sum(
        8.0 for assignment in result.assignments if assignment.employee_id == "vacant-01"
    )
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert worked_hours == 320.0
    assert len(result.assignments) == 40


def test_cpsat_mla_de_l07_catalog_surplus_within_grace_when_feasible() -> None:
    from lab_scheduler.scheduling.contract_payroll import (
        apply_catalog_targets_for_vacant_master_lines,
        build_solver_target_hours_map,
    )

    employee = EmployeeProfile(
        "portage-mla-11",
        "Vacant MLA D/E - Line 07",
        0.6,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)
    payroll = build_solver_target_hours_map(
        [employee],
        rules=MANITOBA,
        weeks_in_period=8,
    )
    catalog = apply_catalog_targets_for_vacant_master_lines(
        [employee],
        payroll,
        rules=MANITOBA,
        weeks_in_period=8,
        period_start=period_start,
        period_end=period_end,
    )

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=[employee],
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=payroll,
        catalog_target_hours=catalog,
        qual_codes={"portage-mla-11": "MLA"},
        time_limit_seconds=60.0,
        portage_daily_band_caps=False,
        compliance_first=True,
    )

    worked_hours = sum(
        8.0 for assignment in result.assignments if assignment.employee_id == "portage-mla-11"
    )
    catalog_hours = float(catalog["portage-mla-11"])
    payroll_hours = float(payroll["portage-mla-11"])
    catalog_surplus = worked_hours - catalog_hours
    payroll_surplus = worked_hours - payroll_hours

    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert worked_hours <= payroll_hours + 0.01
    assert payroll_surplus <= 0.01


def test_cpsat_parttime_vacant_hard_payroll_ceiling_blocks_overtime() -> None:
    """Part-time vacant lines cannot exceed payroll target even under compliance-first."""
    employee = EmployeeProfile(
        "portage-mla-13",
        "Vacant MLA D/E - Line 09",
        0.2,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=13)
    payroll_hours = 32.0

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=[employee],
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"portage-mla-13": payroll_hours},
        qual_codes={"portage-mla-13": "MLA"},
        time_limit_seconds=30.0,
        portage_daily_band_caps=False,
        compliance_first=True,
    )

    worked_hours = sum(
        8.0 for assignment in result.assignments if assignment.employee_id == "portage-mla-13"
    )
    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert worked_hours <= payroll_hours + 0.01


def test_cpsat_open_cells_are_optional_without_unfilled_constraint() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "lab_scheduler"
        / "solver"
        / "cpsat_fill.py"
    ).read_text(encoding="utf-8")
    assert "unfilled_escalated_vars" not in source
    assert "sum(slot_vars) + unfilled == 1" not in source


def test_cpsat_vacant_fillable_grid_is_full_period_not_baseline_limited() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLA D/E - Line 01",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)
    template_assignments = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-morning" if index % 2 == 0 else "shift-evening",
            assignment_date=period_start + timedelta(days=index),
        )
        for index in range(40)
    ]

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=template_assignments,
        employee_target_hours={"vacant-01": 320.0},
        qual_codes={"vacant-01": "MLA"},
        time_limit_seconds=30.0,
    )

    assert result.fillable_slot_count == 56
    worked_hours = sum(8.0 for assignment in result.assignments)
    assert worked_hours >= 320.0


def test_prepare_vacant_lines_preserves_master_rotation_before_cpsat() -> None:
    from lab_scheduler.compliance import MANITOBA
    from lab_scheduler.scheduling.auto_generate import (
        AutoGenerateResult,
        PlannedAssignment,
        _EmployeeState,
        _prepare_vacant_lines_for_cpsat_fill,
    )
    from lab_scheduler.compliance.engine import ShiftTemplateInfo
    from lab_scheduler.solver.cpsat_fill import vacant_portage_employee_ids

    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLA D/E - Line 01",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "named-01",
            "Joanne Example",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
    ]
    shift_templates = {
        "shift-morning": ShiftTemplateInfo(
            id="shift-morning",
            code="MORNING",
            name="Morning",
            start_time="07:00",
            end_time="15:00",
            duration_minutes=480,
            crosses_midnight=False,
        )
    }
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=55)

    result = AutoGenerateResult()
    result.assignments.extend(
        PlannedAssignment("vacant-01", "shift-morning", period_start + timedelta(days=index))
        for index in range(20)
    )
    result.assignments.append(
        PlannedAssignment("named-01", "shift-morning", period_start)
    )
    states = {
        "vacant-01": _EmployeeState(profile=employees[0], target_hours=320.0, total_hours=160.0),
        "named-01": _EmployeeState(profile=employees[1], target_hours=320.0, total_hours=8.0),
    }

    removed = _prepare_vacant_lines_for_cpsat_fill(
        result,
        states,
        employees=employees,
        target_hours_map={"vacant-01": 320.0, "named-01": 320.0},
        shift_templates=shift_templates,
        rules=MANITOBA,
    )

    assert removed == 0
    assert len(result.assignments) == 21
    assert states["vacant-01"].total_hours == 160.0
    assert vacant_portage_employee_ids(employees) == {"vacant-01"}


def _max_consecutive_work_days(
    assignments: Sequence[PlannedAssignment],
    *,
    employee_id: str,
) -> int:
    from lab_scheduler.compliance.engine import _consecutive_work_day_streaks

    work_dates = sorted(
        {assignment.assignment_date for assignment in assignments if assignment.employee_id == employee_id}
    )
    if not work_dates:
        return 0
    return max(length for _start, _end, length in _consecutive_work_day_streaks(work_dates))


def _max_consecutive_nights(
    assignments: Sequence[PlannedAssignment],
    *,
    employee_id: str,
) -> int:
    night_dates = sorted(
        {
            assignment.assignment_date
            for assignment in assignments
            if assignment.employee_id == employee_id
            and _band_code(assignment.shift_template_id) == "NIGHT"
        }
    )
    if not night_dates:
        return 0
    from lab_scheduler.compliance.engine import _consecutive_work_day_streaks

    return max(length for _start, _end, length in _consecutive_work_day_streaks(night_dates))


def test_cpsat_enforces_portage_six_day_work_limit() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLA D/E - Line 08",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=13)

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"vacant-01": 112.0},
        qual_codes={"vacant-01": "MLA"},
        time_limit_seconds=15.0,
    )

    assert _max_consecutive_work_days(result.assignments, employee_id="vacant-01") <= 6


def test_cpsat_enforces_portage_four_night_limit() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/N - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        )
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=13)

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"vacant-01": 112.0},
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=15.0,
    )

    assert _max_consecutive_nights(result.assignments, employee_id="vacant-01") <= 4


def test_shift_equity_metrics_emitted_for_portage_pool() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/N - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        ),
        EmployeeProfile(
            "vacant-03",
            "Vacant MLT D/N - Line 03",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/N",
        ),
    ]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=6)
    target_hours = {"vacant-01": 40.0, "vacant-03": 40.0}

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={"vacant-01": "MLT", "vacant-03": "MLT"},
        time_limit_seconds=15.0,
    )

    metrics = result.shift_equity_metrics
    assert "MLT_D_N_Pool" in metrics
    pool = metrics["MLT_D_N_Pool"]
    assert "target_avg_nights" in pool
    assert "line_01" in pool
    assert "line_03" in pool
    assert "total_D" in pool["line_01"]
    assert "total_N" in pool["line_01"]
    assert "variance_from_avg" in pool["line_01"]
    assert "alternate_shift_pct" in pool["line_01"]


def test_compute_employee_alternate_shift_share_de_and_dn() -> None:
    templates = _templates()
    de_assignments = [
        PlannedAssignment("emp-de", "shift-morning", date(2026, 6, 1)),
        PlannedAssignment("emp-de", "shift-morning", date(2026, 6, 2)),
        PlannedAssignment("emp-de", "shift-evening", date(2026, 6, 3)),
    ]
    de_share = compute_employee_alternate_shift_share(
        "emp-de",
        contract_line_type="D/E",
        assignments=de_assignments,
        shift_templates=templates,
    )
    assert de_share is not None
    assert de_share["alternate_shifts"] == 1
    assert de_share["total_shifts"] == 3
    assert de_share["alternate_shift_pct"] == pytest.approx(33.3, abs=0.1)
    assert de_share["day_shift_pct"] == pytest.approx(66.7, abs=0.1)

    dn_assignments = [
        PlannedAssignment("emp-dn", "shift-morning", date(2026, 6, 1)),
        PlannedAssignment("emp-dn", "shift-night", date(2026, 6, 2)),
        PlannedAssignment("emp-dn", "shift-night", date(2026, 6, 3)),
        PlannedAssignment("emp-dn", "shift-night", date(2026, 6, 4)),
    ]
    dn_share = compute_employee_alternate_shift_share(
        "emp-dn",
        contract_line_type="D/N",
        assignments=dn_assignments,
        shift_templates=templates,
    )
    assert dn_share is not None
    assert dn_share["alternate_shift_pct"] == 75.0
    assert dn_share["alternate_band"] == "N"


def test_format_shift_equity_metrics_summary() -> None:
    metrics = {
        "MLT_D_N_Pool": {
            "target_avg_nights": 18,
            "pool_avg_alternate_shift_pct": 45.0,
            "line_01": {
                "total_D": 22,
                "total_N": 18,
                "variance_from_avg": "0",
                "alternate_shift_pct": 45.0,
            },
            "line_03": {
                "total_D": 15,
                "total_N": 25,
                "variance_from_avg": "+7",
                "alternate_shift_pct": 62.5,
            },
        },
        "MLA_D_E_Pool": {
            "target_avg_evenings": 12,
            "pool_avg_alternate_shift_pct": 30.0,
            "line_02": {
                "total_D": 28,
                "total_E": 12,
                "variance_from_avg": "0",
                "alternate_shift_pct": 30.0,
            },
            "line_05": {
                "total_D": 20,
                "total_E": 20,
                "variance_from_avg": "+8",
                "alternate_shift_pct": 50.0,
            },
        },
    }

    summary = format_shift_equity_metrics_summary(metrics)

    assert "MLT_D_N_Pool (avg 18 nights, avg 45.0% alt)" in summary
    assert "line_01 on target (45.0% alt)" in summary
    assert "line_03 +7 nights (62.5% alt)" in summary
    assert "MLA_D_E_Pool (avg 12 evenings, avg 30.0% alt)" in summary
    assert "line_05 +8 evenings (50.0% alt)" in summary

    rows = alternate_shift_rows_from_equity_metrics(metrics)
    assert len(rows) == 4
    pct_by_line = {row["line"]: row["alternate_shift_pct"] for row in rows}
    assert pct_by_line["MLT D N Pool 01"] == 45.0


def _employee_band_on_date(
    assignments: Sequence[object],
    *,
    employee_id: str,
    assignment_date: date,
) -> str | None:
    for assignment in assignments:
        if (
            assignment.employee_id == employee_id
            and assignment.assignment_date == assignment_date
        ):
            return _band_code(assignment.shift_template_id)
    return None


def _active_weekend_count(
    assignments: Sequence[object],
    *,
    employee_id: str,
    period_start: date,
    period_end: date,
) -> int:
    active = 0
    for assignment_date in _period_dates(period_start, period_end):
        if assignment_date.weekday() != 5:
            continue
        if _employee_band_on_date(
            assignments,
            employee_id=employee_id,
            assignment_date=assignment_date,
        ) is not None:
            active += 1
    return active


def test_cpsat_weekend_mirror_matches_sat_and_sun_per_band() -> None:
    employees = [
        EmployeeProfile(
            "vacant-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        )
    ]
    period_start = date(2026, 6, 6)  # Saturday
    period_end = date(2026, 6, 7)  # Sunday

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours={"vacant-01": 16.0},
        qual_codes={"vacant-01": "MLT"},
        time_limit_seconds=15.0,
    )

    sat_band = _employee_band_on_date(
        result.assignments,
        employee_id="vacant-01",
        assignment_date=period_start,
    )
    sun_band = _employee_band_on_date(
        result.assignments,
        employee_id="vacant-01",
        assignment_date=period_end,
    )
    assert sat_band == sun_band


def test_cpsat_fulltime_lines_have_exactly_two_active_weekends() -> None:
    from lab_scheduler.engine.constraints import portage_employee_target_hours
    from lab_scheduler.simulation.portage_blueprint import build_portage_blueprint_roster

    employees = build_portage_blueprint_roster()
    period_start = date(2026, 6, 1)  # Monday
    period_end = period_start + timedelta(days=55)  # 8 weeks
    target_hours = portage_employee_target_hours(
        employees,
        weeks_in_period=8,
        rules=MANITOBA,
    )

    result = solve_vacant_unassigned_slots(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        time_limit_seconds=120.0,
    )

    assert result.status in {"OPTIMAL", "FEASIBLE"}
    for employee in employees:
        target = float(target_hours[employee.id])
        active = _active_weekend_count(
            result.assignments,
            employee_id=employee.id,
            period_start=period_start,
            period_end=period_end,
        )
        if target >= 312.0:
            assert active == 2, f"{employee.id} expected 2 active weekends, got {active}"
        elif target > 0:
            assert active <= 4, f"{employee.id} exceeded PT weekend cap with {active}"


def test_cpsat_fill_result_exposes_fairness_telemetry_fields() -> None:
    employees = _vacant_de_lines(1)
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=13)
    target_hours = {employees[0].id: 64.0}

    result = _solve_without_band_caps(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=2,
        employees=employees,
        shift_templates=_templates(),
        fixed_assignments=[],
        employee_target_hours=target_hours,
        qual_codes={employees[0].id: "MLT"},
        time_limit_seconds=30.0,
        fairness_weight_scale=1.0,
    )

    assert result.status in {"OPTIMAL", "FEASIBLE"}
    assert result.evening_cluster_slack_total >= 0
    assert result.post_night_recovery_slack_total >= 0
    assert result.fairness_penalty_total == (
        WEIGHT_EVENING_CLUSTER * result.evening_cluster_slack_total
        + WEIGHT_POST_NIGHT_RECOVERY * result.post_night_recovery_slack_total
    )


def test_cpsat_fairness_weight_scale_scales_penalty_total() -> None:
    employees = _vacant_de_lines(1)
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=13)
    target_hours = {employees[0].id: 64.0}
    kwargs = {
        "rules": MANITOBA,
        "period_start": period_start,
        "period_end": period_end,
        "weeks_in_period": 2,
        "employees": employees,
        "shift_templates": _templates(),
        "fixed_assignments": [],
        "employee_target_hours": target_hours,
        "qual_codes": {employees[0].id: "MLT"},
        "time_limit_seconds": 30.0,
        "portage_daily_band_caps": False,
    }

    baseline = solve_vacant_unassigned_slots(**kwargs, fairness_weight_scale=1.0)
    scaled = solve_vacant_unassigned_slots(**kwargs, fairness_weight_scale=2.0)

    assert baseline.evening_cluster_slack_total == scaled.evening_cluster_slack_total
    assert baseline.post_night_recovery_slack_total == scaled.post_night_recovery_slack_total
    assert scaled.fairness_penalty_total == baseline.fairness_penalty_total * 2


def test_cpsat_evening_cluster_objective_reduces_slack_on_skewed_fixture() -> None:
    employee = _vacant_de_lines(1)[0]
    period_start = date(2026, 6, 1)
    period_end = period_start + timedelta(days=13)
    clustered_evenings = [
        PlannedAssignment(employee.id, "shift-evening", period_start + timedelta(days=offset))
        for offset in range(4)
    ]
    kwargs = {
        "rules": MANITOBA,
        "period_start": period_start,
        "period_end": period_end,
        "weeks_in_period": 2,
        "employees": [employee],
        "shift_templates": _templates(),
        "employee_target_hours": {employee.id: 64.0},
        "qual_codes": {employee.id: "MLT"},
        "time_limit_seconds": 30.0,
        "portage_daily_band_caps": False,
    }
    loose = solve_vacant_unassigned_slots(
        **kwargs,
        fixed_assignments=clustered_evenings,
        fairness_thresholds=FairnessThresholds(evening_cluster_max=99),
        fairness_weight_scale=1.0,
    )
    strict = solve_vacant_unassigned_slots(
        **kwargs,
        fixed_assignments=clustered_evenings,
        fairness_thresholds=DEFAULT_FAIRNESS_THRESHOLDS,
        fairness_weight_scale=2.0,
    )

    assert loose.evening_cluster_slack_total >= strict.evening_cluster_slack_total
    assert strict.coverage_shortfall_total == 0
    assert loose.coverage_shortfall_total == 0
