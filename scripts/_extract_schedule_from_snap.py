"""Extract all employee patterns from a snapshot DB for comparison."""
from __future__ import annotations

import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PERIOD_START = date(2026, 6, 1)
DAYS = 56


def pattern_for(conn: sqlite3.Connection, employee_id: str) -> str:
    rows = conn.execute(
        """
        SELECT assignment_date, shift_template_id
        FROM shift_assignments
        WHERE employee_id=? AND schedule_period_id='period-2026-summer'
        ORDER BY assignment_date
        """,
        (employee_id,),
    ).fetchall()
    mapping = {"shift-morning": "D", "shift-evening": "E", "shift-night": "N"}
    by = {date.fromisoformat(d): mapping.get(t, "?") for d, t in rows}
    return "".join(by.get(PERIOD_START + timedelta(i), "-") for i in range(DAYS))


def main() -> None:
    snap = ROOT / sys.argv[1] if len(sys.argv) > 1 else ROOT / "snapshots/20260529T124612Z_pre-auto-pilot-period-2026-summer.sqlite3"
    conn = sqlite3.connect(snap)
    employees = conn.execute(
        """
        SELECT id, first_name, last_name, contract_line_type
        FROM employees WHERE tenant_id='tenant-northstar-lab' AND is_active=1
        ORDER BY contract_line_type, first_name, last_name
        """
    ).fetchall()
    for eid, fn, ln, clt in employees:
        label = f"{fn} {ln}"
        pat = pattern_for(conn, eid)
        print(f"{eid}\t{label}\t{clt}\t{pat}")
    conn.close()


if __name__ == "__main__":
    main()
