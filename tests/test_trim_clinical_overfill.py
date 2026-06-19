
import pytest

pytestmark = pytest.mark.legacy

"""Regression: trim evening/night overfill including vacant Portage lines."""

from datetime import date

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import PlannedAssignment, _trim_clinical_band_overfill
from lab_scheduler.scheduling.auto_generate import _EmployeeState
from lab_scheduler.scheduling.profiles import EmployeeProfile


def _templates() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-evening": ShiftTemplateInfo(
            id="shift-evening",
            code="EVENING",
            name="Evening",
            start_time="15:00",
            end_time="23:00",
            duration_minutes=465,
            crosses_midnight=False,
        ),
        "shift-night": ShiftTemplateInfo(
            id="shift-night",
            code="NIGHT",
            name="Night",
            start_time="23:00",
            end_time="07:00",
            duration_minutes=480,
            crosses_midnight=True,
        ),
    }


def test_trim_clinical_band_overfill_removes_excess_vacant_evening() -> None:
    day = date(2026, 6, 3)
    employees = [
        EmployeeProfile(
            "portage-mlt-01",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "portage-mlt-02",
            "Vacant MLT D/E - Line 02",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "portage-mla-01",
            "Vacant MLA D/E - Line 01",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "portage-mla-02",
            "Vacant MLA D/E - Line 02",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
    ]
    qual_codes = {
        "portage-mlt-01": "MLT",
        "portage-mlt-02": "MLT",
        "portage-mla-01": "MLA",
        "portage-mla-02": "MLA",
    }
    states = {employee.id: _EmployeeState(profile=employee, target_hours=320.0) for employee in employees}
    assignments = [
        PlannedAssignment(employee.id, "shift-evening", day) for employee in employees
    ]
    removed = _trim_clinical_band_overfill(
        assignments,
        states,
        employees=employees,
        shift_templates=_templates(),
        qual_codes=qual_codes,
        period_start=day,
        period_end=day,
    )
    evening_count = sum(
        1 for assignment in assignments if assignment.shift_template_id == "shift-evening"
    )
    assert removed == 2
    assert evening_count == 2


def test_trim_clinical_band_overfill_protects_contract_deficit() -> None:
    day = date(2026, 6, 3)
    employees = [
        EmployeeProfile(
            "surplus-mlt",
            "Vacant MLT D/E - Line 01",
            1.0,
            {"qual-mlt"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "deficit-mla",
            "Vacant MLA D/E - Line 02",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
        EmployeeProfile(
            "surplus-mla",
            "Vacant MLA D/E - Line 03",
            1.0,
            {"qual-mla"},
            contract_line_type="D/E",
        ),
    ]
    qual_codes = {
        "surplus-mlt": "MLT",
        "deficit-mla": "MLA",
        "surplus-mla": "MLA",
    }
    states = {
        "surplus-mlt": _EmployeeState(profile=employees[0], target_hours=320.0),
        "deficit-mla": _EmployeeState(profile=employees[1], target_hours=320.0),
        "surplus-mla": _EmployeeState(profile=employees[2], target_hours=320.0),
    }
    states["surplus-mlt"].total_hours = 340.0
    states["deficit-mla"].total_hours = 240.0
    states["surplus-mla"].total_hours = 336.0
    assignments = [
        PlannedAssignment("surplus-mlt", "shift-evening", day),
        PlannedAssignment("deficit-mla", "shift-evening", day),
        PlannedAssignment("surplus-mla", "shift-evening", day),
    ]
    removed = _trim_clinical_band_overfill(
        assignments,
        states,
        employees=employees,
        shift_templates=_templates(),
        qual_codes=qual_codes,
        period_start=day,
        period_end=day,
        fulltime_target=320.0,
    )
    remaining_ids = {assignment.employee_id for assignment in assignments}
    assert removed == 1
    assert "deficit-mla" in remaining_ids
    assert len(remaining_ids) == 2
