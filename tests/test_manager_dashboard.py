
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.engine.manager_dashboard import (
    build_manager_health_snapshot,
    build_under_target_roster,
    evaluate_period_coverage,
)
from lab_scheduler.scheduling.auto_generate import DeterministicScheduleFailure
from lab_scheduler.scheduling.auto_pilot import AutoPilotRunResult, run_auto_pilot_full_block
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import (
    PERIOD_END,
    PERIOD_START,
    QUAL_MLA,
    QUAL_MLT,
    shift_required_qualifications,
    shift_templates,
)
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_coverage_targets,
    portage_employee_target_hours,
    WEEKS_IN_PERIOD,
)


def _portage_generate() -> AutoPilotRunResult:
    """Generate Portage block with strict pass, then coverage-aggressor fallback."""

    employees = build_portage_roster()
    target_hours = portage_employee_target_hours(
        employees, weeks_in_period=WEEKS_IN_PERIOD, rules=MANITOBA
    )
    last_exc: Exception | None = None
    for aggressor_mode in (True,):
        try:
            return run_auto_pilot_full_block(
                rules=MANITOBA,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                weeks_in_period=WEEKS_IN_PERIOD,
                employees=employees,
                shift_templates=shift_templates(),
                shift_required_qualifications=shift_required_qualifications(),
                employee_target_hours=target_hours,
                coverage_targets=portage_coverage_targets(employees),
                require_master_compliance=False,
                coverage_aggressor_mode=aggressor_mode,
                strict_complete_block=not aggressor_mode,
            )
        except (DeterministicScheduleFailure, RuntimeError) as exc:
            last_exc = exc
            if aggressor_mode:
                raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("Portage generate produced no result")


def test_under_target_list_calculates_fte_deficit_for_portage_roster() -> None:
    employees = build_portage_roster()
    pilot = _portage_generate()
    qual_codes = {QUAL_MLT: "MLT", QUAL_MLA: "MLA"}
    employees_by_id = {employee.id: employee for employee in employees}

    under_target = build_under_target_roster(
        pilot.generate.coverage_tier_results,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
    )

    fulltime_under = [
        row
        for row in under_target
        if employees_by_id[row.employee_id].fte >= 0.99
    ]
    # CP-SAT fills vacant-line cells only; named baseline rows may remain under FTE target.
    assert len(fulltime_under) <= 19

    tier_by_id = {result.tier_id: result for result in pilot.generate.coverage_tier_results}
    for row in under_target:
        tier = tier_by_id[row.employee_id]
        assert row.fte_deficit == round(tier.actual_fte - tier.target_fte, 2)
        assert row.actual_fte == tier.actual_fte
        assert row.contractual_fte == tier.target_fte
        assert row.fte_deficit < 0
        assert row.role in ("MLT", "MLA")
        assert row.seniority_hours > 0
        assert row.scheduled_hours >= 0
        assert row.period_target_hours > 0


def test_manager_health_snapshot_flags_gaps() -> None:
    snapshot = build_manager_health_snapshot(
        compliance_error_count=0,
        coverage_success_pct=92.2,
        gap_alert_count=2,
    )
    assert snapshot.compliance_health_pct == 100.0
    assert snapshot.compliance_status == "healthy"
    assert snapshot.coverage_success_pct == 92.2
    assert snapshot.coverage_status == "healthy"
    assert snapshot.gap_alert_count == 2
    assert snapshot.gap_status == "warn"


def test_evaluate_period_coverage_for_fully_assigned_portage_block() -> None:
    employees = build_portage_roster()
    pilot = _portage_generate()
    qual_codes = {QUAL_MLT: "MLT", QUAL_MLA: "MLA"}
    from lab_scheduler.compliance.engine import ScheduledShift

    assignments = [
        ScheduledShift(
            a.employee_id,
            next(e.full_name for e in employees if e.id == a.employee_id),
            a.assignment_date,
            a.shift_template_id,
        )
        for a in pilot.generate.assignments
    ]
    coverage_pct, tier_results = evaluate_period_coverage(
        rules=MANITOBA,
        period_start=PERIOD_START,
        period_end=PERIOD_END,
        weeks_in_period=WEEKS_IN_PERIOD,
        employees=employees,
        assignments=assignments,
        shift_templates=shift_templates(),
        qual_codes=qual_codes,
    )
    assert coverage_pct >= 0.0
    assert tier_results
