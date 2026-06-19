"""Count schedule export rows the same way compile_period does."""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("LAB_SCHEDULER_QUIET", "1")

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.portage_template import portage_roster_sort_key
from lab_scheduler.scheduling.portage_ui_autopilot import (
    PortageAutoPilotRunConfig,
    default_scheduling_policy,
    run_portage_auto_pilot_ladder,
)
from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows
from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.simulation.hospital_stress import shift_required_qualifications, shift_templates
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_coverage_targets,
    portage_employee_target_hours,
)

period_start = date(2026, 6, 1)
period_end = date(2026, 7, 26)
weeks = 8
employees = list(build_portage_roster())
templates = shift_templates()
target_hours = portage_employee_target_hours(employees, weeks_in_period=weeks, rules=MANITOBA)
pilot, _, _ = run_portage_auto_pilot_ladder(
    PortageAutoPilotRunConfig(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks,
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=target_hours,
        availability_blocked={},
        coverage_targets=portage_coverage_targets(employees),
        scheduling_policy=default_scheduling_policy(),
        archetype=ScheduleArchetype.STANDARD.value,
        emit_triage=True,
        project_root=ROOT,
    )
)
dates = [period_start + timedelta(days=i) for i in range((period_end - period_start).days + 1)]
template_dict = {
    shift_id: {
        "id": shift_id,
        "code": template.code,
        "short": template.code[:1],
        "name": template.name,
    }
    for shift_id, template in templates.items()
}
emp_rows = [
    {
        "id": employee.id,
        "full_name": employee.full_name,
        "fte": employee.fte,
        "contract_line_type": employee.contract_line_type,
    }
    for employee in sorted(employees, key=portage_roster_sort_key)
]
assignment_rows = [
    {
        "employee_id": assignment.employee_id,
        "assignment_date": assignment.assignment_date,
        "shift_template_id": assignment.shift_template_id,
    }
    for assignment in pilot.generate.assignments
]
rows = build_schedule_export_rows(
    emp_rows,
    dates,
    assignment_rows,
    template_dict,
    include_daily_tallies=True,
)
print("schedule_rows", len(rows))
for row in rows[-5:]:
    print(row.get("Employee"), row.get("employee_id"))
