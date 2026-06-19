"""

Run full Portage auto-pilot generation and overwrite the live demo SQLite schedule.



Performs a hard wipe of all tenant employees, provisions the exact 25-line blueprint,

then writes a compliant 4-week schedule and verifies the export row count.



Usage:

    python scripts/sync_portage_live_db.py

    python scripts/sync_portage_live_db.py --db path/to/demo.sqlite3

"""



from __future__ import annotations



import argparse

import csv

import sqlite3

import sys

from datetime import date, datetime, timedelta, timezone

from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]

SRC = ROOT / "src"

if str(SRC) not in sys.path:

    sys.path.insert(0, str(SRC))



from lab_scheduler.compliance import MANITOBA  # noqa: E402

from lab_scheduler.engine.demand import (  # noqa: E402

    find_day_night_transition_violations,

    portage_expanded_slot_total,

)

from lab_scheduler.models.employee import ensure_contract_line_schema  # noqa: E402

from lab_scheduler.scheduling.auto_pilot import (  # noqa: E402

    AutoPilotError,

    persist_auto_pilot_schedule,

    run_auto_pilot_full_block,

)

from lab_scheduler.scheduling.portage_template import portage_roster_sort_key  # noqa: E402

from lab_scheduler.scheduling.schedule_tallies import is_daily_tally_row  # noqa: E402
from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows  # noqa: E402
from lab_scheduler.scheduling.schedule_export import prepend_aggressive_fill_flags_to_export_rows  # noqa: E402
from lab_scheduler.scheduling.schedule_export import is_aggressive_fill_flag_row  # noqa: E402

from lab_scheduler.simulation.hospital_stress import (  # noqa: E402

    QUAL_MLA,

    QUAL_MLT,

    shift_required_qualifications,

    shift_templates,

)

from lab_scheduler.simulation.load_test import (  # noqa: E402

    portage_coverage_targets,

    portage_employee_target_hours,

)

from lab_scheduler.simulation.portage_blueprint import (  # noqa: E402

    PORTAGE_LINE_SPECS,

    PORTAGE_ROSTER_SIZE,

    build_portage_blueprint_roster,

    portage_vacant_line_name,

)



NORTHSTAR_TENANT_ID = "tenant-northstar-lab"

DEFAULT_PERIOD_ID = "period-2026-summer"

PERIOD_START = date(2026, 6, 1)

PERIOD_END = date(2026, 7, 26)

WEEKS_IN_PERIOD = 8

ROSTER_RESET_MARKER = ROOT / ".roster_reset_epoch"

EXPORT_CSV_PATH = ROOT / "exports" / "schedule_period-2026-summer_8.csv"





def _utc_now_iso() -> str:

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")





def _qualification_ids(conn: sqlite3.Connection, tenant_id: str) -> dict[str, str]:

    rows = conn.execute(

        """

        SELECT id, code FROM qualifications

        WHERE tenant_id = ? AND is_active = 1

        """,

        (tenant_id,),

    ).fetchall()

    by_code = {str(code).upper(): str(qual_id) for qual_id, code in rows}

    mapping: dict[str, str] = {}

    if "MLT" in by_code:

        mapping[QUAL_MLT] = by_code["MLT"]

    if "MLA" in by_code:

        mapping[QUAL_MLA] = by_code["MLA"]

    elif "LA" in by_code:

        mapping[QUAL_MLA] = by_code["LA"]

    return mapping





def hard_reset_tenant_employees(

    conn: sqlite3.Connection,

    *,

    tenant_id: str,

    schedule_period_id: str,

) -> int:

    """

    Physically remove every employee row (and dependents) for the tenant.



    Returns the number of employees removed.

    """



    conn.execute("PRAGMA foreign_keys = ON;")



    removed = conn.execute(

        "SELECT COUNT(*) FROM employees WHERE tenant_id = ?",

        (tenant_id,),

    ).fetchone()[0]



    conn.execute(

        "DELETE FROM shift_assignments WHERE tenant_id = ? AND schedule_period_id = ?",

        (tenant_id, schedule_period_id),

    )

    conn.execute(

        "DELETE FROM shift_assignments WHERE tenant_id = ?",

        (tenant_id,),

    )

    conn.execute(

        "DELETE FROM availability_exceptions WHERE tenant_id = ?",

        (tenant_id,),

    )

    conn.execute(

        "DELETE FROM employee_qualifications WHERE tenant_id = ?",

        (tenant_id,),

    )

    conn.execute(

        "DELETE FROM schedule_audit_logs WHERE tenant_id = ?",

        (tenant_id,),

    )

    conn.execute(

        "DELETE FROM sys_audit_log WHERE tenant_id = ?",

        (tenant_id,),

    )

    conn.execute(

        "DELETE FROM employees WHERE tenant_id = ?",

        (tenant_id,),

    )



    conn.execute(

        """

        DELETE FROM sqlite_sequence

        WHERE name IN ('sys_audit_log', 'schedule_audit_logs')

        """

    )



    return int(removed)





def vacuum_database(db_path: Path) -> None:

    """Reclaim space and reset SQLite internal bookkeeping after a hard wipe."""



    conn = sqlite3.connect(str(db_path))

    try:

        conn.execute("VACUUM;")

        conn.commit()

    finally:

        conn.close()





def write_roster_reset_marker() -> str:

    """Signal running Streamlit sessions to drop cached roster widget state."""



    epoch = _utc_now_iso()

    ROSTER_RESET_MARKER.write_text(epoch, encoding="utf-8")

    return epoch





def _ensure_shift_qualifications(

    conn: sqlite3.Connection,

    tenant_id: str,

    qual_ids: dict[str, str],

) -> None:

    now = _utc_now_iso()

    for shift_id in ("shift-morning", "shift-evening", "shift-night"):

        for qual_id in qual_ids.values():

            exists = conn.execute(

                """

                SELECT 1 FROM shift_template_qualifications

                WHERE tenant_id = ? AND shift_template_id = ? AND qualification_id = ?

                """,

                (tenant_id, shift_id, qual_id),

            ).fetchone()

            if exists:

                continue

            conn.execute(

                """

                INSERT INTO shift_template_qualifications (

                  tenant_id, shift_template_id, qualification_id, created_at

                ) VALUES (?, ?, ?, ?)

                """,

                (tenant_id, shift_id, qual_id, now),

            )





def _insert_blueprint_employee(

    conn: sqlite3.Connection,

    *,

    tenant_id: str,

    employee,

    hire_date: date,

    qual_ids: dict[str, str],

) -> None:

    role = "MLT" if QUAL_MLT in employee.qualification_ids else "MLA"

    # full_name is "Vacant MLT D/N - Line 01" — split for DB first/last columns.

    name_parts = employee.full_name.rsplit(" - Line ", 1)

    if len(name_parts) == 2:

        first_name = f"{name_parts[0]} - Line"

        last_name = name_parts[1]

    else:

        first_name = employee.full_name

        last_name = "01"

    now = _utc_now_iso()



    conn.execute(

        """

        INSERT INTO employees (

          id, tenant_id, employee_code, first_name, last_name,

          hire_date, fte, base_hourly_rate, seniority_hours, contract_line_type,

          is_active, created_at, updated_at

        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)

        """,

        (

            employee.id,

            tenant_id,

            employee.id.replace("portage-", "PG-").upper(),

            first_name,

            last_name,

            hire_date.isoformat(),

            employee.fte,

            employee.base_hourly_rate,

            employee.seniority_hours,

            employee.contract_line_type,

            now,

            now,

        ),

    )

    sim_qual = QUAL_MLT if QUAL_MLT in employee.qualification_ids else QUAL_MLA

    qual_id = qual_ids[sim_qual]

    conn.execute(

        """

        INSERT INTO employee_qualifications (

          tenant_id, employee_id, qualification_id, awarded_on, expires_on, created_at

        ) VALUES (?, ?, ?, ?, NULL, ?)

        """,

        (tenant_id, employee.id, qual_id, hire_date.isoformat(), now),

    )





def _fetch_db_employee_rows(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:

    rows = conn.execute(

        """

        SELECT id, first_name, last_name, fte, contract_line_type

        FROM employees

        WHERE tenant_id = ? AND is_active = 1

        ORDER BY id

        """,

        (tenant_id,),

    ).fetchall()

    return [

        {

            "id": row[0],

            "full_name": f"{row[1]} {row[2]}".strip(),

            "fte": float(row[3]),

            "contract_line_type": row[4],

        }

        for row in rows

    ]





def _verify_blueprint_names(roster) -> None:

    expected_names = {portage_vacant_line_name(spec) for spec in PORTAGE_LINE_SPECS}

    actual_names = {employee.full_name for employee in roster}

    missing = expected_names - actual_names

    extra = actual_names - expected_names

    if missing or extra:

        raise RuntimeError(

            f"Blueprint name mismatch. Missing: {sorted(missing)}. Extra: {sorted(extra)}."

        )





def _export_and_verify_schedule_csv(

    *,

    employees: list[dict],

    assignments,

    templates: dict,

    export_path: Path,
    aggressive_fill_flags=None,

) -> int:

    dates = [

        PERIOD_START + timedelta(days=offset)

        for offset in range((PERIOD_END - PERIOD_START).days + 1)

    ]

    template_dict = {

        shift_id: {

            "id": shift_id,

            "code": template.code,

            "short": template.code,

            "name": template.name,

        }

        for shift_id, template in templates.items()

    }

    sorted_employees = sorted(employees, key=portage_roster_sort_key)

    assignment_rows = [

        {

            "employee_id": assignment.employee_id,

            "assignment_date": assignment.assignment_date,

            "shift_template_id": assignment.shift_template_id,

        }

        for assignment in assignments

    ]

    schedule_rows = build_schedule_export_rows(

        sorted_employees,

        dates,

        assignment_rows,

        template_dict,

        include_daily_tallies=True,

    )
    if aggressive_fill_flags:
        schedule_rows = prepend_aggressive_fill_flags_to_export_rows(
            schedule_rows,
            aggressive_fill_flags,
        )

    employee_export_rows = [
        row
        for row in schedule_rows
        if not is_daily_tally_row(row) and not is_aggressive_fill_flag_row(row)
    ]

    if len(employee_export_rows) != PORTAGE_ROSTER_SIZE:

        raise RuntimeError(

            f"Export employee row count {len(employee_export_rows)} != {PORTAGE_ROSTER_SIZE} blueprint lines"

        )



    export_path.parent.mkdir(parents=True, exist_ok=True)

    if schedule_rows:

        fieldnames = list(schedule_rows[0].keys())

        with export_path.open("w", newline="", encoding="utf-8") as handle:

            writer = csv.DictWriter(handle, fieldnames=fieldnames)

            writer.writeheader()

            writer.writerows(schedule_rows)



    return len(schedule_rows)





def sync_portage_live_db(db_path: Path) -> None:

    conn = sqlite3.connect(str(db_path))

    conn.row_factory = sqlite3.Row

    try:

        conn.execute("PRAGMA foreign_keys = ON;")

        ensure_contract_line_schema(conn)



        period = conn.execute(

            """

            SELECT id, period_start, period_end_inclusive, week_count

            FROM schedule_periods

            WHERE tenant_id = ? AND id = ?

            """,

            (NORTHSTAR_TENANT_ID, DEFAULT_PERIOD_ID),

        ).fetchone()

        if period is None:

            raise RuntimeError(

                f"Schedule period {DEFAULT_PERIOD_ID!r} not found for {NORTHSTAR_TENANT_ID!r}. "

                "Run the app once to seed demo.sqlite3."

            )



        qual_ids = _qualification_ids(conn, NORTHSTAR_TENANT_ID)

        if QUAL_MLT not in qual_ids:

            raise RuntimeError("MLT qualification not found in database")



        removed = hard_reset_tenant_employees(

            conn,

            tenant_id=NORTHSTAR_TENANT_ID,

            schedule_period_id=period["id"],

        )

        conn.commit()



        remaining = conn.execute(

            "SELECT COUNT(*) FROM employees WHERE tenant_id = ?",

            (NORTHSTAR_TENANT_ID,),

        ).fetchone()[0]

        if remaining != 0:

            raise RuntimeError(

                f"Hard reset failed: {remaining} employee row(s) still present after DELETE"

            )



        _ensure_shift_qualifications(conn, NORTHSTAR_TENANT_ID, qual_ids)



        roster = build_portage_blueprint_roster()

        _verify_blueprint_names(roster)

        assert len(roster) == PORTAGE_ROSTER_SIZE

        hire_date = date(2024, 1, 1)

        for employee in roster:

            _insert_blueprint_employee(

                conn,

                tenant_id=NORTHSTAR_TENANT_ID,

                employee=employee,

                hire_date=hire_date,

                qual_ids=qual_ids,

            )

        conn.commit()



        active_count = conn.execute(

            "SELECT COUNT(*) FROM employees WHERE tenant_id = ? AND is_active = 1",

            (NORTHSTAR_TENANT_ID,),

        ).fetchone()[0]

        if active_count != PORTAGE_ROSTER_SIZE:

            raise RuntimeError(

                f"Expected exactly {PORTAGE_ROSTER_SIZE} active employees, found {active_count}"

            )



        templates = shift_templates()

        expected_slots = portage_expanded_slot_total(

            period_start=PERIOD_START,

            period_end=PERIOD_END,

            shift_templates=templates,

        )



        try:
            pilot = run_auto_pilot_full_block(
                rules=MANITOBA,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                weeks_in_period=WEEKS_IN_PERIOD,
                employees=roster,
                shift_templates=templates,
                shift_required_qualifications=shift_required_qualifications(),
                employee_target_hours=portage_employee_target_hours(
                    roster,
                    weeks_in_period=WEEKS_IN_PERIOD,
                    rules=MANITOBA,
                ),
                coverage_targets=portage_coverage_targets(roster),
                require_master_compliance=True,
                coverage_aggressor_mode=True,
            )
        except AutoPilotError as exc:
            detail = ", ".join(conflict.manager_label for conflict in exc.conflicts[:5])
            report = exc.conflict_report_path or "exports/Conflict_Report_*.json"
            raise RuntimeError(
                f"Master Schedule blocked by ComplianceValidator: {exc}. "
                f"Conflicts: {detail or 'see conflict report'}. Report: {report}"
            ) from exc



        if pilot.generate.slots_total != expected_slots:

            raise RuntimeError(

                f"Slot matrix mismatch: expected {expected_slots}, got {pilot.generate.slots_total}"

            )



        scan_rows = [

            (a.employee_id, a.assignment_date, a.shift_template_id)

            for a in pilot.generate.assignments

        ]

        transition_violations = find_day_night_transition_violations(scan_rows, templates)

        if transition_violations:

            raise RuntimeError(

                f"Generated schedule contains {len(transition_violations)} illegal D→N transitions"

            )



        inserted = persist_auto_pilot_schedule(

            conn,

            tenant_id=NORTHSTAR_TENANT_ID,

            schedule_period_id=period["id"],

            assignments=pilot.generate.assignments,

            replace_existing=True,

        )

        conn.commit()



        db_employees = _fetch_db_employee_rows(conn, NORTHSTAR_TENANT_ID)

        export_rows = _export_and_verify_schedule_csv(

            employees=db_employees,

            assignments=pilot.generate.assignments,

            templates=templates,

            export_path=EXPORT_CSV_PATH,
            aggressive_fill_flags=pilot.generate.aggressive_fill_flags,

        )



        reset_epoch = write_roster_reset_marker()

        mla_morning = sum(
            1
            for assignment in pilot.generate.assignments
            if assignment.shift_template_id == "shift-morning"
            and assignment.employee_id.startswith("portage-mla")
        )
        print(f"Database: {db_path}")
        print(f"Hard-wiped employees: {removed}")
        print(f"Active vacant lines: {active_count} (target {PORTAGE_ROSTER_SIZE})")
        print(f"Demand matrix seats: {expected_slots}")
        print(f"Assignments written: {inserted}")
        print(f"Fill: {pilot.generate.slots_filled}/{pilot.generate.slots_total}")
        print(f"MLA Day (Morning) assignments: {mla_morning}")
        print(f"Export CSV rows: {export_rows} -> {EXPORT_CSV_PATH}")
        print(f"Roster reset marker: {reset_epoch}")
        print(pilot.proof.success_message())
    finally:
        conn.close()

    vacuum_database(db_path)





def main() -> int:

    parser = argparse.ArgumentParser(description="Sync Portage schedule into live demo DB")

    parser.add_argument(

        "--db",

        type=Path,

        default=ROOT / "demo.sqlite3",

        help="SQLite database path (default: demo.sqlite3)",

    )

    args = parser.parse_args()

    sync_portage_live_db(args.db.resolve())

    return 0





if __name__ == "__main__":

    raise SystemExit(main())


