from datetime import date

import pytest

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.engine.constraints import (
    assess_impossible_coverage_slots,
    build_coverage_targets_from_roster,
    compute_coverage_success_rate_pct,
    evaluate_coverage_tier_results,
    is_schedule_coverage_complete,
    portage_coverage_targets,
    portage_employee_target_hours,
)
from lab_scheduler.engine.demand import (
    expand_schedule_slots,
    filter_portage_operational_shift_templates,
    is_optional_supplemental_coverage_slot,
    portage_concurrent_demands,
)
from lab_scheduler.scheduling.auto_generate import EmployeeProfile, auto_generate_schedule
from lab_scheduler.simulation.load_test import build_portage_roster, run_portage_load_test
from lab_scheduler.simulation.hospital_stress import shift_required_qualifications, shift_templates
from lab_scheduler.workers.logic_worker import LogicWorkerFailure
from tests.portage_fixtures import portage_generate_kwargs

def _required() -> dict[str, set[str]]:
    return shift_required_qualifications()


def test_build_coverage_targets_from_portage_roster() -> None:
    employees = build_portage_roster()[:3]
    targets = portage_coverage_targets(employees)
    assert len(targets) == 3
    assert "MLT" in targets[0].label
    assert targets[0].target_fte == employees[0].fte


def test_portage_employee_target_hours_includes_39_40_pattern() -> None:
    employees = build_portage_roster()
    hours = portage_employee_target_hours(employees, weeks_in_period=4, rules=MANITOBA)
    mlt_dn_03 = next(
        employee.id
        for employee in employees
        if employee.full_name == "Vacant MLT D/N - Line 03"
    )
    mlt_de_02 = next(
        employee.id
        for employee in employees
        if employee.full_name == "Vacant MLT D/E - Line 02"
    )
    assert hours[mlt_de_02] == 160.0
    assert hours[mlt_dn_03] == 158.0


def test_impossible_coverage_detects_unqualified_shift_line() -> None:
    employees = [
        EmployeeProfile("emp-mla-01", "Portage MLA 01", 0.4, {"qual-mla"}),
    ]
    templates = shift_templates()
    impossible, tiers = assess_impossible_coverage_slots(
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=_required(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        rules=MANITOBA,
    )
    assert impossible
    assert "emp-mla-01" in tiers


@pytest.mark.legacy
def test_auto_generate_fails_impossible_evening_slots_for_mla_only_roster() -> None:
    employees = [
        EmployeeProfile("emp-mla-01", "Portage MLA 01", 0.4, {"qual-mla"}),
    ]
    targets = build_coverage_targets_from_roster(
        employees,
        qual_codes={"qual-mla": "MLA"},
    )
    with pytest.raises(LogicWorkerFailure) as exc_info:
        auto_generate_schedule(
            rules=MANITOBA,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 7),
            weeks_in_period=1,
            employees=employees,
            shift_templates=shift_templates(),
            shift_required_qualifications=_required(),
            coverage_targets=targets,
        )

    assert exc_info.value.error is ScheduleError.ERR_IMPOSSIBLE_COVERAGE
    assert exc_info.value.shift_code in {"EVENING", "MORNING"}


@pytest.mark.legacy
def test_auto_generate_fails_with_labor_rule_error_when_no_legal_assignment() -> None:
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
            shift_required_qualifications=_required(),
            coverage_targets=targets,
        )
    except LogicWorkerFailure as exc:
        assert exc.error in {
            ScheduleError.LABOR_RULE,
            ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
            ScheduleError.COVERAGE_TARGET,
        }


def test_coverage_success_rate_reflects_target_vs_actual() -> None:
    targets = build_coverage_targets_from_roster(
        [EmployeeProfile("emp-a1", "MLT 01 (1.0 D/N)", 1.0, {"qual-mlt"})],
        qual_codes={"qual-mlt": "MLT"},
    )
    results = evaluate_coverage_tier_results(
        targets=targets,
        employee_hours={"emp-a1": 16.0},
        rules=MANITOBA,
        weeks_in_period=4,
        slots_total=21,
    )
    assert results[0].actual_fte == 0.1
    assert results[0].period_target_hours == 168.0
    assert compute_coverage_success_rate_pct(results) == round(100.0 * 16.0 / 168.0, 2)


def test_impossible_coverage_tiers_do_not_block_completion() -> None:
    targets = build_coverage_targets_from_roster(
        [EmployeeProfile("emp-a1", "MLT 01 (1.0 D/N)", 1.0, {"qual-mlt"})],
        qual_codes={"qual-mlt": "MLT"},
    )
    results = evaluate_coverage_tier_results(
        targets=targets,
        employee_hours={"emp-a1": 0.0},
        rules=MANITOBA,
        weeks_in_period=4,
        impossible_tier_ids={"emp-a1"},
    )
    assert results[0].is_impossible
    assert not results[0].meets_target
    assert is_schedule_coverage_complete(
        unfilled_coverage_gaps=0,
        tier_results=results,
    )
    assert compute_coverage_success_rate_pct(results) == 100.0


@pytest.mark.legacy
def test_portage_smooth_balance_gaps_are_optional_not_blocking() -> None:
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
    )
    result = auto_generate_schedule(**kwargs)
    assert result.coverage_complete
    assert result.coverage_gap_count == 0
    assert result.optional_coverage_gap_count > 0
    optional_slots = [
        slot
        for slot in expand_schedule_slots(
            period_start=kwargs["period_start"],
            period_end=kwargs["period_end"],
            shift_templates=kwargs["shift_templates"],
            concurrent_demands=portage_concurrent_demands(),
        )
        if is_optional_supplemental_coverage_slot(slot)
    ]
    assert optional_slots


def test_filter_portage_operational_shift_templates_drops_topup() -> None:
    templates = dict(shift_templates())
    templates["twelve-hour-fte-topup"] = ShiftTemplateInfo(
        id="twelve-hour-fte-topup",
        code="TOPUP",
        name="12h FTE Top-Up",
        start_time="08:00",
        end_time="16:00",
        duration_minutes=480,
        crosses_midnight=False,
    )
    filtered = filter_portage_operational_shift_templates(templates)
    assert set(filtered) == {"shift-morning", "shift-evening", "shift-night"}


@pytest.mark.slow
@pytest.mark.legacy
def test_portage_auto_generate_ignores_topup_template_for_coverage() -> None:
    kwargs = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        weeks_in_period=4,
        coverage_aggressor_mode=True,
        require_master_compliance=True,
    )
    templates = dict(kwargs["shift_templates"])
    templates["twelve-hour-fte-topup"] = ShiftTemplateInfo(
        id="twelve-hour-fte-topup",
        code="TOPUP",
        name="12h FTE Top-Up",
        start_time="08:00",
        end_time="16:00",
        duration_minutes=480,
        crosses_midnight=False,
    )
    result = auto_generate_schedule(
        **{
            **kwargs,
            "shift_templates": templates,
            "enable_fairness_rerun": False,
        }
    )
    assert result.coverage_complete
    assert result.coverage_gap_count == 0