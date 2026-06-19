from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.scheduling.agency_fulfillment import (
    AGENCY_PLACEHOLDER_DISPLAY,
    apply_agency_placements_to_schedule_rows,
    persist_agency_placeholder_assignment,
    resolve_employee_id_for_slot,
    shift_template_id_for_code,
)
from lab_scheduler.scheduling.agency_worker import (
    STATUS_FULFILLED,
    STATUS_SENT,
    create_line_item_placeholders,
    mark_agency_request_fulfilled,
    mark_agency_request_sent,
    run_agency_worker,
)


def _write_triage(tmp_path: Path) -> Path:
    path = tmp_path / "exports" / "Triage_Escalation_2026-05-27.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "period_start": "2026-06-01",
                "period_end": "2026-06-28",
                "triage_list": [
                    {
                        "slot": "Vacant MLT D/E - Line 05",
                        "date": "2026-06-03",
                        "shift_code": "MORNING",
                        "blocked_by": ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
                    },
                    {
                        "slot": "Vacant MLT D/E - Line 06",
                        "date": "2026-06-03",
                        "shift_code": "MORNING",
                        "blocked_by": ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_resolve_employee_id_for_slot_matches_full_name() -> None:
    employee_id = resolve_employee_id_for_slot(
        "Vacant MLT D/E - Line 05",
        [{"id": "portage-mlt-05", "full_name": "Vacant MLT D/E - Line 05"}],
    )
    assert employee_id == "portage-mlt-05"


def test_apply_agency_placements_to_schedule_rows() -> None:
    rows = [
        {
            "Employee": "Vacant MLT D/E - Line 05",
            "employee_id": "portage-mlt-05",
            "2026-06-03": "—",
        }
    ]
    tagged = apply_agency_placements_to_schedule_rows(
        rows,
        [
            {
                "mapped_employee_id": "portage-mlt-05",
                "assignment_date": "2026-06-03",
                "assignee_label": "Agency - TBD",
            }
        ],
        [date(2026, 6, 3)],
    )
    assert tagged[0]["2026-06-03"] == AGENCY_PLACEHOLDER_DISPLAY


def test_create_line_item_placeholders_updates_request(tmp_path: Path) -> None:
    triage_path = _write_triage(tmp_path)
    result = run_agency_worker(
        tmp_path,
        triage_path,
        report_date=date(2026, 5, 27),
        schedule_period_id="period-2026-summer",
    )
    assert result.request_path is not None
    mark_agency_request_sent(tmp_path, result.request_path)

    employees = [
        {"id": "portage-mlt-05", "full_name": "Vacant MLT D/E - Line 05"},
        {"id": "portage-mlt-06", "full_name": "Vacant MLT D/E - Line 06"},
    ]
    templates = {"shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "M"}}

    persisted: list[tuple[str, str, date]] = []

    def _persist(employee_id: str, shift_template_id: str, assignment_day: date) -> str:
        persisted.append((employee_id, shift_template_id, assignment_day))
        return f"asg-{employee_id}"

    line_item_id_value = "2026-06-03|MORNING|MLT"
    payload = create_line_item_placeholders(
        tmp_path,
        result.request_path,
        line_item_id_value=line_item_id_value,
        employees=employees,
        templates=templates,
        persist_assignment=_persist,
        actor="manager",
    )

    item = payload["line_items"][0]
    assert len(item["placements"]) == 2
    assert item["fulfillment_status"] == "FULFILLED"
    assert persisted[0][0] == "portage-mlt-05"
    assert shift_template_id_for_code(templates, "MORNING") == "shift-morning"


def test_mark_agency_request_fulfilled(tmp_path: Path) -> None:
    triage_path = _write_triage(tmp_path)
    result = run_agency_worker(tmp_path, triage_path, report_date=date(2026, 5, 27))
    assert result.request_path is not None
    mark_agency_request_sent(tmp_path, result.request_path)
    payload = mark_agency_request_fulfilled(
        tmp_path,
        result.request_path,
        vendor_reference="LOC-4421",
        actor="manager",
    )
    assert payload["status"] == STATUS_FULFILLED
    assert payload["vendor_reference"] == "LOC-4421"


def test_persist_agency_placeholder_assignment_sqlite() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE tenants (id TEXT PRIMARY KEY);
        CREATE TABLE employees (tenant_id TEXT, id TEXT, PRIMARY KEY (tenant_id, id));
        CREATE TABLE shift_templates (tenant_id TEXT, id TEXT, code TEXT, PRIMARY KEY (tenant_id, id));
        CREATE TABLE schedule_periods (
          tenant_id TEXT, id TEXT, period_start TEXT, period_end_inclusive TEXT,
          PRIMARY KEY (tenant_id, id)
        );
        CREATE TABLE shift_assignments (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          schedule_period_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          shift_template_id TEXT NOT NULL,
          assignment_date TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          system_note TEXT
        );
        INSERT INTO tenants VALUES ('tenant-a');
        INSERT INTO employees VALUES ('tenant-a', 'portage-mlt-05');
        INSERT INTO shift_templates VALUES ('tenant-a', 'shift-morning', 'MORNING');
        INSERT INTO schedule_periods VALUES ('tenant-a', 'period-1', '2026-06-01', '2026-06-28');
        """
    )
    assignment_id = persist_agency_placeholder_assignment(
        conn,
        tenant_id="tenant-a",
        schedule_period_id="period-1",
        employee_id="portage-mlt-05",
        shift_template_id="shift-morning",
        assignment_date=date(2026, 6, 3),
    )
    row = conn.execute(
        "SELECT system_note FROM shift_assignments WHERE id = ?",
        (assignment_id,),
    ).fetchone()
    assert row is not None
    assert "AGENCY_PLACEHOLDER" in row[0]
