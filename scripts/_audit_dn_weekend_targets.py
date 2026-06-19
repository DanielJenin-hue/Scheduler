"""Print catalog weekend shift targets for Portage D/N vacant lines."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["LAB_SCHEDULER_QUIET"] = "1"

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.scheduling.contract_payroll import apply_catalog_targets_for_vacant_master_lines
from lab_scheduler.scheduling.portage_equity_targets import build_vacant_line_weekend_target_map
from lab_scheduler.scheduling.portage_template import vacant_master_catalog_period_weekend_shifts
from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours

period_start = date(2026, 6, 1)
period_end = date(2026, 7, 26)
employees = list(build_portage_roster())
payroll = portage_employee_target_hours(employees, weeks_in_period=8, rules=MANITOBA)
catalog = apply_catalog_targets_for_vacant_master_lines(
    employees,
    payroll,
    rules=MANITOBA,
    weeks_in_period=8,
    period_start=period_start,
    period_end=period_end,
)
qual_codes = {employee.id: infer_qual_code(employee) for employee in employees}
targets = build_vacant_line_weekend_target_map(
    employees,
    catalog,
    qual_codes,
    period_start=period_start,
    period_end=period_end,
)

print("D/N vacant lines — stamped vs pool-scaled weekend targets:")
for employee in employees:
    if (employee.contract_line_type or "").upper() != "D/N":
        continue
    stamped = vacant_master_catalog_period_weekend_shifts(
        employee,
        period_start,
        period_end,
    )
    target = targets.get(employee.id, 0)
    print(f"  {employee.full_name}: stamped={stamped} target={target}")
