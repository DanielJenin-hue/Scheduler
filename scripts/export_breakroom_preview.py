"""Generate a breakroom HTML preview from the Portage 8-week twelve-hour auto-pilot schedule."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.auto_pilot import run_auto_pilot_full_block
from lab_scheduler.scheduling.breakroom_print import generate_breakroom_print_html
from lab_scheduler.scheduling.portage_template import portage_roster_sort_key
from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows
from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import FTE_TOPUP_TEMPLATE_ID
from lab_scheduler.simulation.hospital_stress import shift_required_qualifications, shift_templates
from lab_scheduler.simulation.load_test import (
    build_portage_roster,
    portage_coverage_targets,
    portage_employee_target_hours,
)


def main() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    weeks_in_period = 8
    employees = list(build_portage_roster())
    templates = shift_templates()

    pilot = run_auto_pilot_full_block(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=portage_employee_target_hours(
            employees,
            weeks_in_period=weeks_in_period,
            rules=MANITOBA,
        ),
        coverage_targets=portage_coverage_targets(employees),
        require_master_compliance=False,
        archetype=ScheduleArchetype.TWELVE_HOUR.value,
    )

    dates = [
        period_start + timedelta(days=offset)
        for offset in range((period_end - period_start).days + 1)
    ]
    template_dict = {
        shift_id: {
            "id": shift_id,
            "code": template.code,
            "short": template.code,
            "name": template.name,
        }
        for shift_id, template in templates.items()
    }
    template_dict[FTE_TOPUP_TEMPLATE_ID] = {
        "id": FTE_TOPUP_TEMPLATE_ID,
        "code": "TOPUP",
        "short": "T",
        "name": "FTE Top-up Shift",
    }

    emp_rows = sorted(
        [
            {
                "id": employee.id,
                "full_name": employee.full_name,
                "fte": employee.fte,
                "contract_line_type": employee.contract_line_type,
            }
            for employee in employees
        ],
        key=portage_roster_sort_key,
    )

    assignment_rows = [
        {
            "employee_id": assignment.employee_id,
            "assignment_date": assignment.assignment_date,
            "shift_template_id": assignment.shift_template_id,
        }
        for assignment in pilot.generate.assignments
    ]
    schedule_rows = build_schedule_export_rows(
        emp_rows,
        dates,
        assignment_rows,
        template_dict,
    )

    html = generate_breakroom_print_html(
        facility_name="Northstar Medical Laboratory",
        period_name="Summer 2026 Master Rotation",
        period_start=period_start,
        period_end=period_end,
        week_count=weeks_in_period,
        employees=emp_rows,
        dates=dates,
        schedule_rows=schedule_rows,
        aggressive_fill_flags=pilot.generate.aggressive_fill_flags,
        schedule_archetype=ScheduleArchetype.TWELVE_HOUR.value,
    )

    output = ROOT / "exports" / "breakroom_schedule_period-2026-summer_9.html"
    output.write_text(html, encoding="utf-8")
    downloads_copy = Path.home() / "Downloads" / "breakroom_schedule_period-2026-summer.html"
    downloads_copy.write_text(html, encoding="utf-8")
    print(f"Wrote {output}")
    print(f"Wrote {downloads_copy}")
    print(pilot.proof.success_message())
    print(f"Assignments: {pilot.generate.slots_filled}/{pilot.generate.slots_total}")


if __name__ == "__main__":
    main()
