from __future__ import annotations

from datetime import date
from typing import Any

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.engine.constraints import portage_coverage_targets, portage_employee_target_hours
from lab_scheduler.simulation.hospital_stress import shift_required_qualifications, shift_templates
from lab_scheduler.simulation.load_test import build_portage_roster


def portage_generate_kwargs(
    *,
    period_start: date = date(2026, 6, 1),
    period_end: date = date(2026, 6, 28),
    weeks_in_period: int = 4,
    strict_complete_block: bool = False,
    coverage_aggressor_mode: bool = True,
) -> dict[str, Any]:
    employees = build_portage_roster()
    return {
        "rules": MANITOBA,
        "period_start": period_start,
        "period_end": period_end,
        "weeks_in_period": weeks_in_period,
        "employees": employees,
        "shift_templates": shift_templates(),
        "shift_required_qualifications": shift_required_qualifications(),
        "employee_target_hours": portage_employee_target_hours(
            employees,
            weeks_in_period=weeks_in_period,
            rules=MANITOBA,
        ),
        "coverage_targets": portage_coverage_targets(employees),
        "strict_complete_block": strict_complete_block,
        "coverage_aggressor_mode": coverage_aggressor_mode,
    }
