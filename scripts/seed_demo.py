from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from lab_scheduler.time import workweek_for


ROOT = Path(__file__).resolve().parents[1]
SQL_DIR = ROOT / "sql"


@dataclass(frozen=True)
class Employee:
    id: str
    code: str
    first_name: str
    last_name: str
    fte: float

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"


@dataclass(frozen=True)
class ShiftTemplate:
    id: str
    code: str
    name: str
    duration_minutes: int


@dataclass(frozen=True)
class Assignment:
    employee_id: str
    shift_template_id: str
    assignment_date: date


def _load_schema_and_seed(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # Minimal base tenants table, then our SQL migrations and seed.
    cur.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS tenants (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          slug TEXT NOT NULL UNIQUE,
          status TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )

    for fname in ("03_lab_core_tables.sql", "04_schedule_periods_and_assignments.sql", "seed_demo_lab.sql"):
        script_path = SQL_DIR / fname
        cur.executescript(script_path.read_text(encoding="utf-8"))

    conn.commit()


def _fetch_demo_period_bounds(conn: sqlite3.Connection, tenant_id: str, period_id: str) -> Tuple[date, date]:
    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT period_start, period_end_inclusive
        FROM schedule_periods
        WHERE tenant_id = ? AND id = ?
        """,
        (tenant_id, period_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Schedule period {period_id!r} not found for tenant {tenant_id!r}")
    start, end_inclusive = (date.fromisoformat(row[0]), date.fromisoformat(row[1]))

    # Sanity: enforce Monday-start via Python logic engine as well.
    ww = workweek_for(start)
    if ww.start != start:
        raise RuntimeError(f"Schedule period start {start} is not a Monday per logic engine")

    return start, end_inclusive


def _fetch_employees(conn: sqlite3.Connection, tenant_id: str) -> Dict[str, Employee]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, employee_code, first_name, last_name, fte
        FROM employees
        WHERE tenant_id = ? AND is_active = 1
        ORDER BY last_name, first_name
        """,
        (tenant_id,),
    ).fetchall()
    return {
        r[0]: Employee(id=r[0], code=r[1] or "", first_name=r[2], last_name=r[3], fte=float(r[4]))
        for r in rows
    }


def _fetch_shift_templates(conn: sqlite3.Connection, tenant_id: str) -> Dict[str, ShiftTemplate]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, code, name, duration_minutes
        FROM shift_templates
        WHERE tenant_id = ? AND is_active = 1
        """,
        (tenant_id,),
    ).fetchall()
    return {
        r[0]: ShiftTemplate(id=r[0], code=r[1], name=r[2], duration_minutes=int(r[3]))
        for r in rows
    }


def _fetch_assignments(
    conn: sqlite3.Connection, tenant_id: str, period_id: str
) -> List[Assignment]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT employee_id, shift_template_id, assignment_date
        FROM shift_assignments
        WHERE tenant_id = ? AND schedule_period_id = ?
        ORDER BY assignment_date, employee_id
        """,
        (tenant_id, period_id),
    ).fetchall()
    return [
        Assignment(
            employee_id=r[0],
            shift_template_id=r[1],
            assignment_date=date.fromisoformat(r[2]),
        )
        for r in rows
    ]


def _daterange(start: date, end_inclusive: date) -> Iterable[date]:
    cur = start
    while cur <= end_inclusive:
        yield cur
        cur += timedelta(days=1)


def _compute_hours(
    employees: Dict[str, Employee],
    templates: Dict[str, ShiftTemplate],
    assignments: List[Assignment],
) -> Dict[str, float]:
    minutes_by_employee: Dict[str, int] = defaultdict(int)
    for a in assignments:
        tmpl = templates[a.shift_template_id]
        minutes_by_employee[a.employee_id] += tmpl.duration_minutes
    return {emp_id: minutes / 60.0 for emp_id, minutes in minutes_by_employee.items()}


def _target_hours_for_period(fte: float, weeks: int) -> float:
    # Assumption: 40h/week at 1.0 FTE.
    return fte * 40.0 * weeks


def _print_calendar_grid(
    start: date,
    end_inclusive: date,
    employees: Dict[str, Employee],
    templates: Dict[str, ShiftTemplate],
    assignments: List[Assignment],
) -> None:
    print("=== Demo 4-week schedule (Monday-start) ===")
    print(f"Period: {start.isoformat()} (Mon) to {end_inclusive.isoformat()}")
    print()

    # Build lookup: (employee_id, date) -> shift code
    code_by_emp_date: Dict[Tuple[str, date], str] = {}
    for a in assignments:
        tmpl = templates[a.shift_template_id]
        code_by_emp_date[(a.employee_id, a.assignment_date)] = tmpl.code[0]  # e.g. M/E/N/S

    # One row per employee, one column per date
    dates = list(_daterange(start, end_inclusive))
    header = ["Employee"] + [d.strftime("%m-%d") for d in dates]
    print(" | ".join(header))
    print("-" * (len(header) * 7))

    for emp in employees.values():
        row = [emp.full_name]
        for d in dates:
            ch = code_by_emp_date.get((emp.id, d), ".")
            row.append(ch)
        print(" | ".join(row))

    print()


def _print_labor_summary(
    weeks: int,
    employees: Dict[str, Employee],
    actual_hours: Dict[str, float],
) -> None:
    print("=== Labor compliance summary ===")
    print(f"Assumed baseline: 40h/week at 1.0 FTE over {weeks} weeks")
    print()
    print("Employee | FTE | Target (h) | Scheduled (h) | Delta (h)")
    print("-" * 70)
    for emp in employees.values():
        target = _target_hours_for_period(emp.fte, weeks)
        scheduled = round(actual_hours.get(emp.id, 0.0), 1)
        delta = round(scheduled - target, 1)
        print(
            f"{emp.full_name} | {emp.fte:.1f} | "
            f"{target:5.1f} | {scheduled:5.1f} | {delta:+5.1f}"
        )

    print()


def main() -> None:
    tenant_id = "tenant-northstar-lab"
    period_id = "period-2026-summer"

    conn = sqlite3.connect(":memory:")
    try:
        _load_schema_and_seed(conn)

        start, end_inclusive = _fetch_demo_period_bounds(conn, tenant_id, period_id)
        employees = _fetch_employees(conn, tenant_id)
        templates = _fetch_shift_templates(conn, tenant_id)
        assignments = _fetch_assignments(conn, tenant_id, period_id)

        # Calendar
        _print_calendar_grid(start, end_inclusive, employees, templates, assignments)

        # Hours & compliance
        actual_hours = _compute_hours(employees, templates, assignments)
        weeks = (end_inclusive - start).days // 7 + 1
        _print_labor_summary(weeks, employees, actual_hours)
    finally:
        conn.close()


if __name__ == "__main__":
    main()

