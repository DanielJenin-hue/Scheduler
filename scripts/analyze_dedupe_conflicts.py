"""Analyze employee-day conflicts before/after dedupe."""

from __future__ import annotations

import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_scheduler.compliance import MANITOBA  # noqa: E402
from lab_scheduler.compliance.engine import ShiftTemplateInfo  # noqa: E402
from lab_scheduler.engine.constraints import portage_coverage_targets, portage_employee_target_hours  # noqa: E402
from lab_scheduler.scheduling.auto_pilot import (  # noqa: E402
    dedupe_planned_assignments,
    run_auto_pilot_full_block,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile  # noqa: E402
from lab_scheduler.scheduling.schedule_tallies import (  # noqa: E402
    find_portage_operational_tally_violations,
    shift_band_from_template_code,
)


def main() -> None:
    conn = sqlite3.connect(ROOT / "demo.sqlite3")
    conn.row_factory = sqlite3.Row
    tenant = "tenant-northstar-lab"
    period = conn.execute(
        "SELECT * FROM schedule_periods WHERE id = ?", ("period-2026-summer",)
    ).fetchone()
    emp_quals: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT employee_id, qualification_id FROM employee_qualifications WHERE tenant_id = ?",
        (tenant,),
    ):
        emp_quals.setdefault(row["employee_id"], set()).add(row["qualification_id"])
    raw = {
        row["id"]: dict(row)
        for row in conn.execute("SELECT * FROM shift_templates WHERE tenant_id = ?", (tenant,))
    }
    template_info = {
        tid: ShiftTemplateInfo(
            id=tid,
            code=t["code"],
            name=t["name"],
            start_time=t["start_time"],
            end_time=t["end_time"],
            duration_minutes=int(t["duration_minutes"]),
            crosses_midnight=bool(t["crosses_midnight"]),
        )
        for tid, t in raw.items()
    }
    profiles = [
        EmployeeProfile(
            id=e["id"],
            full_name=f"{e['first_name']} {e['last_name']}".strip(),
            fte=float(e["fte"]),
            qualification_ids=emp_quals.get(e["id"], set()),
            seniority_hours=float(e["seniority_hours"] or 0),
            base_hourly_rate=float(e["base_hourly_rate"] or 40),
            contract_line_type=e["contract_line_type"],
        )
        for e in conn.execute(
            "SELECT * FROM employees WHERE tenant_id = ? AND is_active = 1", (tenant,)
        )
    ]
    shift_quals: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT shift_template_id, qualification_id FROM shift_template_qualifications WHERE tenant_id = ?",
        (tenant,),
    ):
        shift_quals.setdefault(row[0], set()).add(row[1])
    conn.close()

    ps = date.fromisoformat(period["period_start"])
    pe = date.fromisoformat(period["period_end_inclusive"])
    targets = portage_employee_target_hours(
        profiles, weeks_in_period=int(period["week_count"]), rules=MANITOBA
    )
    pilot = run_auto_pilot_full_block(
        rules=MANITOBA,
        period_start=ps,
        period_end=pe,
        weeks_in_period=int(period["week_count"]),
        employees=profiles,
        shift_templates=template_info,
        shift_required_qualifications=shift_quals,
        employee_target_hours=targets,
        coverage_targets=portage_coverage_targets(profiles),
        require_master_compliance=True,
        coverage_aggressor_mode=True,
        enable_fairness_rerun=False,
        emit_triage=False,
    )
    raw_assignments = pilot.generate.assignments
    bands = {
        tid: shift_band_from_template_code(info.code)
        for tid, info in template_info.items()
    }
    by_key: dict[tuple[str, date], list[str]] = defaultdict(list)
    for assignment in raw_assignments:
        band = bands.get(assignment.shift_template_id, "?")
        by_key[(assignment.employee_id, assignment.assignment_date)].append(band)

    conflicts = {key: bands_list for key, bands_list in by_key.items() if len(bands_list) > 1}
    print(f"raw={len(raw_assignments)} deduped={len(dedupe_planned_assignments(raw_assignments))}")
    print(f"employee-day keys with >1 assignment: {len(conflicts)}")
    combo_counter: Counter[tuple[str, ...]] = Counter()
    en_lost = 0
    for bands_list in conflicts.values():
        combo_counter[tuple(sorted(bands_list))] += 1
        if "E" in bands_list or "N" in bands_list:
            if bands_list[-1] in ("D", "?"):
                en_lost += 1
    print("top conflict band combos (last wins in dedupe):", combo_counter.most_common(8))
    print("conflicts where last band is D but E/N also present:", en_lost)

    deduped = dedupe_planned_assignments(raw_assignments)
    viol = find_portage_operational_tally_violations(
        deduped, period_start=ps, period_end=pe, template_id_to_band=bands
    )
    print(f"tally violations after naive dedupe: {len(viol)}")


if __name__ == "__main__":
    main()
