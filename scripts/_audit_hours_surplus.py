"""Per-line hours vs contract targets after generate."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["LAB_SCHEDULER_QUIET"] = "1"
os.environ["LAB_SCHEDULER_SKIP_AGENT_LOG"] = "1"

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.engine.demand import infer_qual_code
from lab_scheduler.scheduling.auto_generate import _EmployeeState, _rebuild_states_from_assignments
from lab_scheduler.scheduling.contract_payroll import apply_catalog_targets_for_vacant_master_lines
from lab_scheduler.scheduling.portage_ui_autopilot import (
    PortageAutoPilotRunConfig,
    default_scheduling_policy,
    run_portage_auto_pilot_ladder,
)
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
payroll = portage_employee_target_hours(employees, weeks_in_period=weeks, rules=MANITOBA)
catalog = apply_catalog_targets_for_vacant_master_lines(
    employees,
    payroll,
    rules=MANITOBA,
    weeks_in_period=weeks,
    period_start=period_start,
    period_end=period_end,
)

pilot, _, _ = run_portage_auto_pilot_ladder(
    PortageAutoPilotRunConfig(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks,
        employees=employees,
        shift_templates=templates,
        shift_required_qualifications=shift_required_qualifications(),
        employee_target_hours=payroll,
        availability_blocked={},
        coverage_targets=portage_coverage_targets(employees),
        scheduling_policy=default_scheduling_policy(),
        emit_triage=False,
        project_root=ROOT,
    )
)
states = {
    e.id: _EmployeeState(profile=e, target_hours=payroll[e.id]) for e in employees
}
_rebuild_states_from_assignments(states, pilot.generate.assignments, templates)

needed = sum(payroll.values())
actual = sum(s.total_hours for s in states.values())
print(f"Net delta (actual - payroll targets): {actual - needed:+.1f}h")
print(f"Coverage gaps: {pilot.generate.coverage_gap_count}")
print(f"Coverage complete: {pilot.generate.coverage_complete}")
print("\nLines over payroll target (+8h band):")
rows = []
for e in employees:
    h = states[e.id].total_hours
    pt = payroll[e.id]
    ct = catalog.get(e.id, 0.0)
    delta = h - pt
    if delta > 8.25:
        rows.append((delta, e.id, e.full_name, h, pt, ct))
for delta, eid, name, h, pt, ct in sorted(rows, reverse=True):
    print(f"  {delta:+.1f}h {eid} sched={h:.0f} payroll={pt:.0f} catalog={ct:.0f} | {name}")

print("\nLines under payroll target (-8h band):")
for e in employees:
    h = states[e.id].total_hours
    pt = payroll[e.id]
    delta = h - pt
    if delta < -8.25:
        print(f"  {delta:+.1f}h {e.id} sched={h:.0f} payroll={pt:.0f} | {e.full_name}")
