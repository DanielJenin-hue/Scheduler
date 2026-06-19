
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

import pytest

from lab_scheduler.scheduling.auto_generate import (
    ImmediateClinicalFailure,
    PlannedAssignment,
    _EmployeeState,
    _enforce_weekend_clinical_floor,
    _enforce_weekend_qual_limits,
    _trim_weekend_morning_overfill,
    _weekend_qual_assignment_counts,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile


def _templates() -> dict[str, object]:
    return {
        "shift-morning": type("T", (), {"id": "shift-morning", "code": "MORNING", "duration_minutes": 480})(),
        "shift-evening": type("T", (), {"id": "shift-evening", "code": "EVENING", "duration_minutes": 480})(),
    }


def test_weekend_qual_counts_detects_missing_mla() -> None:
    saturday = date(2026, 6, 6)
    employees = [
        EmployeeProfile("emp-mlt", "MLT One", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-mla", "MLA One", 1.0, {"qual-mla"}),
    ]
    assignments = [
        PlannedAssignment("emp-mlt", "shift-morning", saturday),
    ]
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    counts = _weekend_qual_assignment_counts(
        assignments,
        employees=employees,
        qual_codes=qual_codes,
        assignment_date=saturday,
        shift_templates=_templates(),
    )
    assert counts["MLT"] == 1
    assert counts["MLA"] == 0


def test_enforce_weekend_clinical_floor_raises_immediate_failure() -> None:
    saturday = date(2026, 6, 6)
    employees = [EmployeeProfile("emp-mlt", "MLT One", 1.0, {"qual-mlt"})]
    assignments = [PlannedAssignment("emp-mlt", "shift-morning", saturday)]
    with pytest.raises(ImmediateClinicalFailure, match="IMMEDIATE CLINICAL FAILURE"):
        _enforce_weekend_qual_limits(
            assignments,
            employees=employees,
            qual_codes={"qual-mlt": "MLT"},
            shift_templates=_templates(),
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 28),
        )


def test_enforce_weekend_clinical_floor_passes_with_mlt_and_mla() -> None:
    saturday = date(2026, 6, 6)
    employees = [
        EmployeeProfile("emp-mlt", "MLT One", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-mla", "MLA One", 1.0, {"qual-mla"}),
    ]
    sunday = date(2026, 6, 7)
    assignments = [
        PlannedAssignment("emp-mlt", "shift-morning", saturday),
        PlannedAssignment("emp-mla", "shift-morning", saturday),
        PlannedAssignment("emp-mla", "shift-evening", saturday),
        PlannedAssignment("emp-mlt", "shift-morning", sunday),
        PlannedAssignment("emp-mla", "shift-morning", sunday),
        PlannedAssignment("emp-mla", "shift-evening", sunday),
    ]
    _enforce_weekend_clinical_floor(
        assignments,
        employees=employees,
        qual_codes={"qual-mlt": "MLT", "qual-mla": "MLA"},
        shift_templates=_templates(),
        period_start=date(2026, 6, 6),
        period_end=date(2026, 6, 7),
    )


def test_trim_weekend_morning_overfill_keeps_one_mla() -> None:
    saturday = date(2026, 6, 6)
    employees = [
        EmployeeProfile("emp-mlt-1", "MLT One", 1.0, {"qual-mlt"}),
        EmployeeProfile("emp-mla-1", "MLA One", 1.0, {"qual-mla"}),
        EmployeeProfile(
            "vacant-01",
            "Vacant MLA D/E - Line 01",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
    ]
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}
    assignments = [
        PlannedAssignment("emp-mlt-1", "shift-morning", saturday),
        PlannedAssignment("emp-mla-1", "shift-morning", saturday),
        PlannedAssignment("vacant-01", "shift-morning", saturday),
    ]
    states = {
        employee.id: _EmployeeState(profile=employee, target_hours=320.0)
        for employee in employees
    }
    removed = _trim_weekend_morning_overfill(
        assignments,
        states,
        employees=employees,
        shift_templates=_templates(),
        qual_codes=qual_codes,
        period_start=saturday,
        period_end=saturday,
    )
    assert removed == 1
    assert len(assignments) == 2
    assert {assignment.employee_id for assignment in assignments} == {
        "emp-mlt-1",
        "emp-mla-1",
    }
    _enforce_weekend_clinical_floor(
        assignments,
        employees=employees,
        qual_codes=qual_codes,
        shift_templates=_templates(),
        period_start=saturday,
        period_end=saturday,
        states=states,
    )
