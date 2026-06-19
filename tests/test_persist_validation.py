from datetime import date, timedelta

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.persist_validation import (
    find_core_persist_violations,
    log_core_persist_violations,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


def _templates() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
        "shift-evening": ShiftTemplateInfo(
            "shift-evening", "EVENING", "Evening", "15:00", "23:00", 480, False
        ),
        "shift-night": ShiftTemplateInfo(
            "shift-night", "NIGHT", "Night", "23:00", "07:00", 480, False
        ),
    }


def test_compliance_first_skips_coverage_and_clinical_gaps() -> None:
    employee = EmployeeProfile(
        "vacant-01",
        "Vacant MLT D/E - Line 01",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    violations = find_core_persist_violations(
        assignments=[],
        employees=[employee],
        shift_templates=_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"vacant-01": "MLT"},
        template_id_to_band={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        coverage_complete=False,
        coverage_gap_count=12,
        clinical_gap_messages=("2026-06-06 EVENING: shortfall",),
        compliance_first=True,
    )
    assert not violations


def test_contract_hours_flags_part_time_one_shift_over() -> None:
    employee = EmployeeProfile(
        "vacant-pt",
        "Vacant MLA D/E - Line 08 (128h)",
        0.5,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    assignments = [
        PlannedAssignment(
            employee_id="vacant-pt",
            shift_template_id="shift-morning",
            assignment_date=date(2026, 6, 1) + timedelta(days=offset),
        )
        for offset in range(17)
    ]
    violations = find_core_persist_violations(
        assignments=assignments,
        employees=[employee],
        shift_templates=_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"vacant-pt": "MLA"},
        template_id_to_band={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        compliance_first=False,
    )
    assert any(item.code == "CONTRACT_HOURS" for item in violations)


def test_contract_hours_allows_part_time_on_catalog_target() -> None:
    from lab_scheduler.scheduling.contract_payroll import (
        apply_catalog_targets_for_vacant_master_lines,
        build_solver_target_hours_map,
    )

    employee = EmployeeProfile(
        "vacant-pt",
        "Vacant MLA D/E - Line 08 (128h)",
        0.5,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    catalog_targets = apply_catalog_targets_for_vacant_master_lines(
        [employee],
        build_solver_target_hours_map(
            [employee],
            rules=MANITOBA,
            weeks_in_period=8,
        ),
        rules=MANITOBA,
        weeks_in_period=8,
        period_start=period_start,
        period_end=period_end,
    )
    target_hours = float(catalog_targets["vacant-pt"])
    shift_count = int(round(target_hours / 8.0))
    assignments = [
        PlannedAssignment(
            employee_id="vacant-pt",
            shift_template_id="shift-morning",
            assignment_date=period_start + timedelta(days=offset * 2),
        )
        for offset in range(shift_count)
        if period_start + timedelta(days=offset * 2) <= period_end
    ]
    violations = find_core_persist_violations(
        assignments=assignments,
        employees=[employee],
        shift_templates=_templates(),
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"vacant-pt": "MLA"},
        template_id_to_band={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        compliance_first=False,
        recompute_clinical_gaps=False,
    )
    assert not any(item.code == "CONTRACT_HOURS" for item in violations)


def test_weekend_shift_drift_flags_part_time_mismatch() -> None:
    employee = EmployeeProfile(
        "vacant-pt",
        "Vacant MLA D/E - Line 08 (128h)",
        0.5,
        {"qual-mla"},
        contract_line_type="D/E",
    )
    assignments = [
        PlannedAssignment(
            employee_id="vacant-pt",
            shift_template_id="shift-morning",
            assignment_date=weekend_day,
        )
        for weekend_day in (
            date(2026, 6, 6),
            date(2026, 6, 7),
            date(2026, 6, 13),
            date(2026, 6, 14),
            date(2026, 6, 20),
            date(2026, 6, 21),
        )
    ]
    violations = find_core_persist_violations(
        assignments=assignments,
        employees=[employee],
        shift_templates=_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 21),
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"vacant-pt": "MLA"},
        template_id_to_band={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        compliance_first=False,
        recompute_clinical_gaps=False,
        coverage_complete=True,
    )
    assert any(item.code == "WEEKEND_SHIFT_DRIFT" for item in violations)


def test_contract_hours_allows_fulltime_at_payroll_target() -> None:
    employee = EmployeeProfile(
        "vacant-01",
        "Vacant MLT D/E - Line 01",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    assignments = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-morning",
            assignment_date=date(2026, 6, 1) + timedelta(days=offset),
        )
        for offset in range(40)
    ]
    violations = find_core_persist_violations(
        assignments=assignments,
        employees=[employee],
        shift_templates=_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"vacant-01": "MLT"},
        template_id_to_band={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        compliance_first=False,
    )
    assert not any(item.code == "CONTRACT_HOURS" for item in violations)


def test_contract_hours_flags_fulltime_catalog_surplus_at_328() -> None:
    employee = EmployeeProfile(
        "vacant-01",
        "Vacant MLT D/E - Line 01",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    assignments = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-morning",
            assignment_date=date(2026, 6, 1) + timedelta(days=offset),
        )
        for offset in range(41)
    ]
    violations = find_core_persist_violations(
        assignments=assignments,
        employees=[employee],
        shift_templates=_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"vacant-01": "MLT"},
        template_id_to_band={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        compliance_first=False,
    )
    contract_violations = [item for item in violations if item.code == "CONTRACT_HOURS"]
    assert contract_violations
    assert "320h contract target" in contract_violations[0].message


def test_ft_vacant_line_328h_scheduled_fails_payroll_target_passes_at_320h() -> None:
    """Category 1 harness: persist blocks 328h vs payroll 320h; 320h passes."""

    employee = EmployeeProfile(
        "vacant-01",
        "Vacant MLA D/N - Line 01",
        1.0,
        {"qual-mla"},
        contract_line_type="D/N",
    )
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    template_bands = {
        "shift-morning": "D",
        "shift-evening": "E",
        "shift-night": "N",
    }
    kwargs = dict(
        employees=[employee],
        shift_templates=_templates(),
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"vacant-01": "MLA"},
        template_id_to_band=template_bands,
        compliance_first=False,
    )
    over_assignments = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-morning",
            assignment_date=period_start + timedelta(days=offset),
        )
        for offset in range(41)
    ]
    at_target_assignments = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-morning",
            assignment_date=period_start + timedelta(days=offset),
        )
        for offset in range(40)
    ]
    over_violations = find_core_persist_violations(
        assignments=over_assignments,
        **kwargs,
    )
    ok_violations = find_core_persist_violations(
        assignments=at_target_assignments,
        **kwargs,
    )
    contract_over = [item for item in over_violations if item.code == "CONTRACT_HOURS"]
    assert contract_over
    assert "320h contract target" in contract_over[0].message
    assert not any(item.code == "CONTRACT_HOURS" for item in ok_violations)


def test_contract_hours_flags_mlt_l09_128h_vs_64h_catalog() -> None:
    """Summer draft offender: MLT D/E L09 at 128h vs 64h catalog/payroll."""

    employee = EmployeeProfile(
        "mlt-de-09",
        "Vacant MLT D/E - Line 09",
        0.2,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    assignments = [
        PlannedAssignment(
            employee_id="mlt-de-09",
            shift_template_id="shift-morning",
            assignment_date=period_start + timedelta(days=offset * 2),
        )
        for offset in range(16)
    ]
    violations = find_core_persist_violations(
        assignments=assignments,
        employees=[employee],
        shift_templates=_templates(),
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"mlt-de-09": "MLT"},
        template_id_to_band={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        compliance_first=False,
    )
    contract_violations = [item for item in violations if item.code == "CONTRACT_HOURS"]
    assert contract_violations
    assert "mlt-de-09" in contract_violations[0].message.lower() or "64h" in contract_violations[0].message


def test_contract_hours_flags_fulltime_large_surplus() -> None:
    employee = EmployeeProfile(
        "vacant-01",
        "Vacant MLT D/E - Line 01",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/E",
    )
    assignments = [
        PlannedAssignment(
            employee_id="vacant-01",
            shift_template_id="shift-morning",
            assignment_date=date(2026, 6, 1) + timedelta(days=offset),
        )
        for offset in range(42)
    ]
    violations = find_core_persist_violations(
        assignments=assignments,
        employees=[employee],
        shift_templates=_templates(),
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
        rules=MANITOBA,
        qual_codes={"vacant-01": "MLT"},
        template_id_to_band={
            "shift-morning": "D",
            "shift-evening": "E",
            "shift-night": "N",
        },
        compliance_first=False,
    )
    codes = {item.code for item in violations}
    assert "CONTRACT_HOURS" in codes
