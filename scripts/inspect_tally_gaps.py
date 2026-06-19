"""Inspect remaining tally violations after band-priority dedupe."""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_scheduler.compliance import MANITOBA  # noqa: E402
from lab_scheduler.compliance.engine import ShiftTemplateInfo  # noqa: E402
from lab_scheduler.engine.constraints import portage_coverage_targets, portage_employee_target_hours  # noqa: E402
from lab_scheduler.scheduling.auto_pilot import dedupe_planned_assignments, run_auto_pilot_full_block  # noqa: E402
from lab_scheduler.scheduling.profiles import EmployeeProfile  # noqa: E402
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code  # noqa: E402


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
    bands = {
        tid: shift_band_from_template_code(info.code)
        for tid, info in template_info.items()
    }
    raw_assignments = pilot.generate.assignments
    deduped = dedupe_planned_assignments(raw_assignments, template_id_to_band=bands)

    for target_day in (date(2026, 6, 15), date(2026, 7, 8)):
        print(f"\n=== {target_day} ===")
        raw_e = [
            a
            for a in raw_assignments
            if a.assignment_date == target_day
            and bands.get(a.shift_template_id) == "E"
        ]
        ded_e = [
            a
            for a in deduped
            if a.assignment_date == target_day
            and bands.get(a.shift_template_id) == "E"
        ]
        print("raw E count", len(raw_e), "deduped E count", len(ded_e))
        by_key: dict[tuple[str, date], list[str]] = defaultdict(list)
        for a in raw_assignments:
            if a.assignment_date != target_day:
                continue
            by_key[(a.employee_id, a.assignment_date)].append(
                bands.get(a.shift_template_id, "?")
            )
        for key, band_list in sorted(by_key.items()):
            if "E" in band_list and len(band_list) > 1:
                winner = next(
                    a
                    for a in deduped
                    if a.employee_id == key[0] and a.assignment_date == key[1]
                )
                win_band = bands.get(winner.shift_template_id, "?")
                print(key[0], band_list, "-> winner", win_band)


if __name__ == "__main__":
    main()
