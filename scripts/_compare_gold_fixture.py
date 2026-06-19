"""Compare DB schedule against gold screenshot fixture."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "portage_manual_screenshot_summer_2026.json"
PERIOD_START = date(2026, 6, 1)
PERIOD_ID = "period-2026-summer"
MAPPING = {"shift-morning": "D", "shift-evening": "E", "shift-night": "N"}


def pattern_from_db(conn: sqlite3.Connection, employee_id: str) -> str:
    rows = conn.execute(
        """
        SELECT assignment_date, shift_template_id
        FROM shift_assignments
        WHERE employee_id=? AND schedule_period_id=?
        ORDER BY assignment_date
        """,
        (employee_id, PERIOD_ID),
    ).fetchall()
    by = {date.fromisoformat(d): MAPPING.get(t, "-") for d, t in rows}
    return "".join(by.get(PERIOD_START + timedelta(i), "-") for i in range(56))


def main() -> None:
    db_path = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "demo.sqlite3")
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    conn = sqlite3.connect(db_path)

    mismatches = 0
    matched = 0
    for employee_id, weeks in payload["employees"].items():
        expected = "".join(weeks)
        actual = pattern_from_db(conn, employee_id)
        if expected != actual:
            mismatches += 1
            print(f"MISMATCH {employee_id}")
            for week_index in range(8):
                exp = expected[week_index * 7 : (week_index + 1) * 7]
                act = actual[week_index * 7 : (week_index + 1) * 7]
                if exp != act:
                    print(f"  W{week_index + 1}: expected {exp}  actual {act}")
        else:
            matched += 1

    conn.close()
    print(f"\n{matched}/{matched + mismatches} employees match gold fixture")
    if mismatches:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
