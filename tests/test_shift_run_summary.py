
import pytest

pytestmark = pytest.mark.legacy

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.auto_generate import PlannedAssignment
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.shift_run_summary import compute_auto_pilot_shift_summary
from lab_scheduler.simulation.hospital_stress import QUAL_MLT, shift_templates as load_shift_templates


def test_compute_auto_pilot_shift_summary_counts_bands() -> None:
    shift_template_map = load_shift_templates()
    morning_id = next(
        shift_id
        for shift_id, template in shift_template_map.items()
        if template.code == "MORNING"
    )
    evening_id = next(
        shift_id
        for shift_id, template in shift_template_map.items()
        if template.code == "EVENING"
    )
    template_info = {
        shift_id: ShiftTemplateInfo(
            id=shift_id,
            code=template.code,
            name=template.name,
            start_time=template.start_time,
            end_time=template.end_time,
            duration_minutes=template.duration_minutes,
            crosses_midnight=template.crosses_midnight,
        )
        for shift_id, template in shift_template_map.items()
    }
    employee = EmployeeProfile(
        id="emp-1",
        full_name="Vacant MLT D/E - Line 01",
        fte=1.0,
        qualification_ids={QUAL_MLT},
        contract_line_type="D/E",
    )
    assignments = [
        PlannedAssignment("emp-1", morning_id, date(2026, 6, 1)),
        PlannedAssignment("emp-1", evening_id, date(2026, 6, 2)),
    ]
    template_bands = {
        shift_id: ("D" if info.code == "MORNING" else "E" if info.code == "EVENING" else "N")
        for shift_id, info in template_info.items()
    }
    summary = compute_auto_pilot_shift_summary(
        assignments=assignments,
        employees=[employee],
        shift_templates=template_info,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        weeks_in_period=1,
        qual_codes={"emp-1": "MLT"},
        template_id_to_band=template_bands,
        required_slots_filled=2,
        required_slots_total=4,
        persist_ok=False,
        employee_target_hours={"emp-1": 320.0},
        rules=MANITOBA,
    )
    assert summary.total_shifts == 2
    assert summary.by_band["D"] == 1
    assert summary.by_band["E"] == 1
    assert summary.per_line[0]["D"] == 1
    assert summary.per_line[0]["E"] == 1
