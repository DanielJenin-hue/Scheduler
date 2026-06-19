"""Build merged fixture: W1-W4 from screenshot transcription, W5-W8 from best DB snap."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "snapshots" / "20260611T184353Z_pre-screenshot-restore-automatic.sqlite3"
OUT = ROOT / "tests" / "fixtures" / "portage_manual_screenshot_summer_2026.json"
PERIOD_START = date(2026, 6, 1)

# W1-W4 transcribed from manager screenshots (2026-06-08). Each string is Mon-Sun.
W1_W4: dict[str, list[str]] = {
    "portage-mlt-05": ["DDDDD--", "DDDDD--", "EEEEE--", "DDDDD--"],
    "portage-mlt-06": ["DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mlt-07": ["DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mlt-08": ["DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mlt-09": ["DDDD-ED", "DDEEE--", "DDDDD--", "DDDDD--"],
    "portage-mlt-10": ["DDDDD--", "DDDDD--", "DD-ED--", "DD-EE--"],
    "portage-mlt-11": ["DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mlt-12": ["DDDDD--", "DDDDD--", "---D---", "DDDDD--"],
    "portage-mlt-13": ["-------", "EE-----", "----EE-", "-------"],
    "portage-mlt-01": ["DDDDDNN", "NNNNN--", "DDDDD--", "DDDDD--"],
    "portage-mlt-02": ["DDDDD--", "DDDDD--", "DDDDDNN", "NNNNN--"],
    "portage-mlt-03": ["NNNNN--", "DDDDD--", "DDDDD--", "DDDDDNN"],
    "portage-mlt-04": ["DDDDD--", "DDDDDNN", "NNNNN--", "DDDDD--"],
    "portage-mla-01": ["EEEEE-E", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mla-02": ["DDDDD--", "DDDDD--", "EEEEE-E", "DDDDD--"],
    "portage-mla-03": ["DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mla-04": ["DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mla-05": ["DDDDD--", "DDDDDE-", "DDDDD--", "DDDDD--"],
    "portage-mla-10": ["DDDDD--", "DDDDD--", "DDDDD--", "EEEEE-E"],
    "portage-mla-11": ["DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mla-12": ["DDDDD--", "DDDDD--", "DDDDD--", "DDDDD--"],
    "portage-mla-06": ["DDDDDNN", "NNNNN--", "DDDDD--", "DDDDD--"],
    "portage-mla-07": ["DDDDD--", "DDDDD--", "DDDDDNN", "NNNNN--"],
    "portage-mla-08": ["NNNNN--", "DDDDD--", "DDDDD--", "DDDDDNN"],
    "portage-mla-09": ["DDDDD--", "DDDDDNN", "NNNNN--", "DDDDD--"],
}


def _pattern_from_snap(conn: sqlite3.Connection, employee_id: str) -> str:
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
    by = {date.fromisoformat(d): mapping.get(t, "-") for d, t in rows}
    return "".join(by.get(PERIOD_START + timedelta(i), "-") for i in range(56))


def _split_weeks(pattern: str) -> list[str]:
    return [pattern[i * 7 : (i + 1) * 7] for i in range(8)]


def main() -> None:
    conn = sqlite3.connect(SNAP)
    employees: dict[str, list[str]] = {}
    for employee_id, weeks_1_4 in W1_W4.items():
        snap_weeks = _split_weeks(_pattern_from_snap(conn, employee_id))
        employees[employee_id] = weeks_1_4 + snap_weeks[4:8]
    conn.close()

    payload = {
        "description": "Summer 2026 manual schedule: W1-W4 from screenshot transcription, W5-W8 from best DB snapshot.",
        "period_id": "period-2026-summer",
        "period_start": "2026-06-01",
        "employees": employees,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
