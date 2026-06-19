"""
RSI (Recursive Strategic Infrastructure) — autonomous project management entry point.

Daily: clinical floor audit, self-correction reports, Value-First dashboard refresh.
Weekly: Prospector viability scan for regional high-volume labs.

Usage:
    python -m auto_manager --db demo.sqlite3 --tenant demo-tenant --period <period-id>
    python -m auto_manager --init
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.rsi.manager import RSIAutoManager


def _default_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _fetch_active_period(conn: sqlite3.Connection, tenant_id: str) -> str:
    row = conn.execute(
        """
        SELECT id
        FROM schedule_periods
        WHERE tenant_id = ?
        ORDER BY period_start DESC
        LIMIT 1
        """,
        (tenant_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"No schedule period found for tenant {tenant_id!r}")
    return str(row[0])


def _fetch_employees(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, first_name, last_name, fte, seniority_hours, base_hourly_rate, contract_line_type
        FROM employees
        WHERE tenant_id = ? AND COALESCE(is_active, 1) = 1
        ORDER BY last_name, first_name
        """,
        (tenant_id,),
    ).fetchall()
    return [
        {
            "id": row[0],
            "full_name": f"{row[1]} {row[2]}",
            "fte": row[3],
            "seniority_hours": row[4],
            "base_hourly_rate": row[5],
            "contract_line_type": row[6],
        }
        for row in rows
    ]


def _fetch_shift_templates(conn: sqlite3.Connection, tenant_id: str) -> dict:
    rows = conn.execute(
        """
        SELECT id, code, name, start_time, end_time, duration_minutes, crosses_midnight
        FROM shift_templates
        WHERE tenant_id = ?
        """,
        (tenant_id,),
    ).fetchall()
    return {
        str(row[0]): {
            "id": row[0],
            "code": row[1],
            "name": row[2],
            "start_time": row[3],
            "end_time": row[4],
            "duration_minutes": row[5],
            "crosses_midnight": bool(row[6]),
        }
        for row in rows
    }


def _fetch_assignments(conn: sqlite3.Connection, tenant_id: str, period_id: str) -> list[dict]:
    has_system_note = any(
        str(col[1]) == "system_note"
        for col in conn.execute("PRAGMA table_info(shift_assignments)").fetchall()
    )
    if has_system_note:
        rows = conn.execute(
            """
            SELECT id, employee_id, shift_template_id, assignment_date, system_note
            FROM shift_assignments
            WHERE tenant_id = ? AND schedule_period_id = ?
              AND COALESCE(assignment_status, 'assigned') = 'assigned'
            """,
            (tenant_id, period_id),
        ).fetchall()
        return [
            {
                "id": row[0],
                "employee_id": row[1],
                "shift_template_id": row[2],
                "assignment_date": date.fromisoformat(str(row[3])),
                "system_note": row[4],
                "forced_clinical_ot": (row[4] or "") == "FORCED_CLINICAL_OT",
            }
            for row in rows
        ]

    rows = conn.execute(
        """
        SELECT id, employee_id, shift_template_id, assignment_date
        FROM shift_assignments
        WHERE tenant_id = ? AND schedule_period_id = ?
          AND COALESCE(assignment_status, 'assigned') = 'assigned'
        """,
        (tenant_id, period_id),
    ).fetchall()
    return [
        {
            "id": row[0],
            "employee_id": row[1],
            "shift_template_id": row[2],
            "assignment_date": date.fromisoformat(str(row[3])),
            "forced_clinical_ot": False,
        }
        for row in rows
    ]


def _fetch_employee_qualifications(conn: sqlite3.Connection, tenant_id: str) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for row in conn.execute(
        """
        SELECT employee_id, qualification_id
        FROM employee_qualifications
        WHERE tenant_id = ?
        """,
        (tenant_id,),
    ):
        mapping.setdefault(str(row[0]), set()).add(str(row[1]))
    return mapping


def _qual_code_map(emp_quals: dict[str, set[str]]) -> dict[str, str]:
    qual_codes: dict[str, str] = {}
    for qual_ids in emp_quals.values():
        for qual_id in qual_ids:
            lowered = qual_id.lower()
            if "mlt" in lowered:
                qual_codes[qual_id] = "MLT"
            elif "mla" in lowered:
                qual_codes[qual_id] = "MLA"
    return qual_codes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RSI autonomous project manager")
    parser.add_argument("--project-root", type=Path, default=_default_project_root())
    parser.add_argument("--db", type=Path, default=_default_project_root() / "demo.sqlite3")
    parser.add_argument("--tenant", type=str, default="demo-tenant")
    parser.add_argument("--period", type=str, default=None)
    parser.add_argument("--init", action="store_true", help="Initialize RSI storage only")
    parser.add_argument("--force-prospector", action="store_true", help="Run weekly Prospector scan now")
    args = parser.parse_args(argv)

    manager = RSIAutoManager(project_root=args.project_root, rules=MANITOBA)
    if args.init:
        root = manager.initialize()
        print(f"RSI initialized at {root}")
        return 0

    if not args.db.is_file():
        raise SystemExit(f"Database not found: {args.db}")

    conn = sqlite3.connect(str(args.db))
    try:
        period_id = args.period or _fetch_active_period(conn, args.tenant)
        employees = _fetch_employees(conn, args.tenant)
        templates = _fetch_shift_templates(conn, args.tenant)
        assignments = _fetch_assignments(conn, args.tenant, period_id)
        emp_quals = _fetch_employee_qualifications(conn, args.tenant)
        qual_map = _qual_code_map(emp_quals)

        target_hours = {
            str(employee["id"]): float(employee.get("fte", 1.0) or 1.0) * 160.0
            for employee in employees
        }

        result = manager.run_self_correction_loop(
            conn,
            tenant_id=args.tenant,
            period_id=period_id,
            employees=employees,
            templates=templates,
            assignments=assignments,
            emp_quals=emp_quals,
            target_hours=target_hours,
            qual_code_map=qual_map,
            today=date.today(),
            force_prospector=args.force_prospector,
        )
    finally:
        conn.close()

    print(
        f"RSI daily cycle complete | reliability={result.dashboard.operational_reliability_pct:.1f}% "
        f"| MRR=${result.dashboard.total_revenue_month_usd:,.0f}/mo "
        f"| clinical_risks={result.project_health.total_clinical_risk_count}"
    )
    if result.risk_report is not None:
        print(
            f"Risk Mitigation Report generated: {result.risk_report.breach_count} breach(es), "
            f"{len(result.risk_report.proposed_swaps)} swap proposal(s)"
        )
    if result.dashboard.next_best_facility_target:
        print(f"Next-Best-Facility-Target: {result.dashboard.next_best_facility_target}")
    if result.prospector_ran:
        print(f"Prospector weekly scan: {len(result.viability_reports)} viability report(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
