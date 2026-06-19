"""RSI gate: footer + rotation invariants on clean-grid ALTERNATE_SHIFTS fill."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _path in (_SRC, _ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.policy.frame_bridge import (
    assignments_from_schedule_frame,
    schedule_frame_row_index_by_employee_id,
)
from lab_scheduler.scheduling.preference_policy import FillMode
from lab_scheduler.scheduling.rotation_invariants import check_rotation_invariants
from lab_scheduler.scheduling.schedule_tallies import find_portage_operational_tally_violations
from lab_scheduler.simulation.load_test import build_portage_roster, portage_employee_target_hours
from tests.test_distribute_alternate_shifts import _period_dates
from tests.test_preference_fill import _db_templates, _fill_specs, _templates


def _template_id_to_band() -> dict[str, str]:
    bands: dict[str, str] = {}
    for template_id, template in _templates().items():
        code = str(template.code or "").strip().upper()
        if code in {"MORNING", "M", "DAY"}:
            bands[str(template_id)] = "D"
        elif code == "EVENING":
            bands[str(template_id)] = "E"
        elif code == "NIGHT":
            bands[str(template_id)] = "N"
    return bands


def main() -> int:
    start = date(2026, 6, 1)
    dates = _period_dates(start)
    roster = build_portage_roster()
    targets = portage_employee_target_hours(roster, weeks_in_period=8, rules=MANITOBA)
    specs = [(e.id, e.full_name, e.contract_line_type or "D/E") for e in roster]
    employees = [{"id": e.id, "full_name": e.full_name, "fte": e.fte} for e in roster]
    frame, result = _fill_specs(
        dates,
        specs,
        targets=targets,
        mode=FillMode.ALTERNATE_SHIFTS,
    )
    row_lookup = schedule_frame_row_index_by_employee_id(frame)
    employees_by_id = {e.id: e for e in roster}
    qual_codes = {"qual-mlt": "MLT", "qual-mla": "MLA"}

    scheduled = assignments_from_schedule_frame(
        frame,
        employees=employees,
        dates=dates,
        templates=_db_templates(),
    )
    tally_violations = find_portage_operational_tally_violations(
        scheduled,
        period_start=start,
        period_end=dates[-1],
        template_id_to_band=_template_id_to_band(),
    )
    from lab_scheduler.scheduling.rotation_invariants import (
        _weekend_evening_is_pt_only_orphan,
    )

    tally_violations = [
        issue
        for issue in tally_violations
        if not (
            issue.band == "E"
            and issue.actual == 1
            and issue.target == 2
            and _weekend_evening_is_pt_only_orphan(
                frame,
                issue.assignment_date,
                row_lookup,
                employees_by_id,
                qual_codes,
                employee_target_hours=targets,
            )
        )
    ]
    report = check_rotation_invariants(
        frame,
        dates=dates,
        row_lookup=row_lookup,
        employees_by_id=employees_by_id,
        qual_codes=qual_codes,
        employee_target_hours=targets,
    )

    print("Tier counts:", dict(result.tier_counts))
    print("Operational tally violations:", len(tally_violations))
    for issue in tally_violations[:10]:
        print(f"  {issue.assignment_date} {issue.band} {issue.actual}/{issue.target}")
    print("Rotation invariant violations:", len(report.violations))
    for violation in report.violations[:15]:
        print(f"  [{violation.invariant_id}] {violation.message}")

    if tally_violations or not report.passed:
        return 1
    print("RSI gate: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
