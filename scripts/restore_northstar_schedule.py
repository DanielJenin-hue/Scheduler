"""Generate from live demo DB using tenant templates (matches Streamlit app)."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lab_scheduler.compliance import MANITOBA  # noqa: E402
from lab_scheduler.compliance.engine import ShiftTemplateInfo  # noqa: E402
from lab_scheduler.engine.constraints import (  # noqa: E402
    portage_coverage_targets,
    portage_employee_target_hours,
)
from lab_scheduler.scheduling.auto_pilot import (  # noqa: E402
    dedupe_planned_assignments,
    persist_auto_pilot_schedule,
    run_auto_pilot_full_block,
)
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code  # noqa: E402
from lab_scheduler.scheduling.profiles import EmployeeProfile  # noqa: E402
from lab_scheduler.simulation.hospital_stress import shift_required_qualifications  # noqa: E402


def _fetch_shift_required_qualification_ids(
    conn: sqlite3.Connection,
    tenant_id: str,
) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for row in conn.execute(
        """
        SELECT shift_template_id, qualification_id
        FROM shift_template_qualifications
        WHERE tenant_id = ?
        """,
        (tenant_id,),
    ):
        mapping.setdefault(row["shift_template_id"], set()).add(row["qualification_id"])
    return mapping


def main() -> None:
    db = ROOT / "demo.sqlite3"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tenant = "tenant-northstar-lab"
    period_id = "period-2026-summer"
    period = conn.execute(
        "SELECT * FROM schedule_periods WHERE id = ?", (period_id,)
    ).fetchone()

    emp_quals: dict[str, set[str]] = {}
    for row in conn.execute(
        "SELECT employee_id, qualification_id FROM employee_qualifications WHERE tenant_id = ?",
        (tenant,),
    ):
        emp_quals.setdefault(row["employee_id"], set()).add(row["qualification_id"])

    raw_templates = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT * FROM shift_templates WHERE tenant_id = ?",
            (tenant,),
        )
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
        for tid, t in raw_templates.items()
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
            "SELECT * FROM employees WHERE tenant_id = ? AND is_active = 1",
            (tenant,),
        )
    ]

    period_start = date.fromisoformat(period["period_start"])
    period_end = date.fromisoformat(period["period_end_inclusive"])
    weeks = int(period["week_count"])
    targets = portage_employee_target_hours(profiles, weeks_in_period=weeks, rules=MANITOBA)
    shift_quals = _fetch_shift_required_qualification_ids(conn, tenant)

    pilot = run_auto_pilot_full_block(
        rules=MANITOBA,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks,
        employees=profiles,
        shift_templates=template_info,
        shift_required_qualifications=shift_quals,
        employee_target_hours=targets,
        coverage_targets=portage_coverage_targets(profiles),
        require_master_compliance=True,
        coverage_aggressor_mode=True,
        enable_fairness_rerun=False,
        emit_triage=True,
    )
    gen = pilot.generate
    print(
        f"coverage_complete={gen.coverage_complete} gaps={gen.coverage_gap_count} "
        f"required={gen.required_slots_filled}/{gen.required_slots_total} "
        f"assignments={len(gen.assignments)}"
    )
    if not gen.coverage_complete:
        conn.close()
        raise SystemExit(1)

    template_bands = {
        template_id: shift_band_from_template_code(info.code)
        for template_id, info in template_info.items()
    }
    assignments = dedupe_planned_assignments(
        gen.assignments,
        template_id_to_band=template_bands,
    )

    inserted = persist_auto_pilot_schedule(
        conn,
        tenant_id=tenant,
        schedule_period_id=period_id,
        assignments=assignments,
        replace_existing=True,
    )
    conn.commit()
    conn.close()
    print(f"persisted {inserted} assignments")


if __name__ == "__main__":
    main()
