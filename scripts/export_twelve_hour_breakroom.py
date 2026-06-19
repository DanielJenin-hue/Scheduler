"""Generate an 8-week 7-on/7-off breakroom HTML export with the fixed twelve-hour pipeline."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"
for path in (str(SRC), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from lab_scheduler.scheduling.auto_generate import auto_generate_schedule
from lab_scheduler.scheduling.breakroom_print import generate_breakroom_print_html
from lab_scheduler.scheduling.portage_template import portage_roster_sort_key
from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows
from lab_scheduler.scheduling.strategies import ScheduleArchetype

from portage_fixtures import portage_generate_kwargs


def main() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    weeks_in_period = 8
    kwargs = portage_generate_kwargs(
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
    )
    result = auto_generate_schedule(**kwargs, archetype=ScheduleArchetype.TWELVE_HOUR.value)
    dates = [period_start + timedelta(days=offset) for offset in range((period_end - period_start).days + 1)]
    templates = kwargs["shift_templates"]
    template_dict = {
        shift_id: {
            "id": shift_id,
            "code": template.code,
            "short": template.code,
            "name": template.name,
        }
        for shift_id, template in templates.items()
    }
    emp_rows = sorted(
        [
            {
                "id": employee.id,
                "full_name": employee.full_name,
                "fte": employee.fte,
                "contract_line_type": employee.contract_line_type or "",
            }
            for employee in kwargs["employees"]
        ],
        key=portage_roster_sort_key,
    )
    assignment_rows = [
        {
            "employee_id": assignment.employee_id,
            "assignment_date": assignment.assignment_date,
            "shift_template_id": assignment.shift_template_id,
        }
        for assignment in result.assignments
    ]
    schedule_rows = build_schedule_export_rows(emp_rows, dates, assignment_rows, template_dict)
    html = generate_breakroom_print_html(
        facility_name="Northstar Medical Laboratory",
        period_name="Summer 2026 Master Rotation",
        period_start=period_start,
        period_end=period_end,
        week_count=weeks_in_period,
        employees=emp_rows,
        dates=dates,
        schedule_rows=schedule_rows,
        schedule_archetype=ScheduleArchetype.TWELVE_HOUR.value,
    )
    root = Path(__file__).resolve().parents[1]
    project_path = root / "exports" / "breakroom_schedule_period-2026-summer_9.html"
    downloads_path = Path.home() / "Downloads" / "breakroom_schedule_period-2026-summer.html"
    project_path.write_text(html, encoding="utf-8")
    downloads_path.write_text(html, encoding="utf-8")
    print(f"Wrote {project_path}")
    print(f"Wrote {downloads_path}")
    print(f"Assignments: {len(result.assignments)}  archetype: {result.schedule_archetype}")


if __name__ == "__main__":
    main()
