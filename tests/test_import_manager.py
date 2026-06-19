import io

import pandas as pd
import pytest

from lab_scheduler.data.import_manager import (
    ExistingEmployeeRecord,
    RosterImportError,
    build_import_preview,
    commit_import_preview,
    fuzzy_match_existing_employee,
    parse_roster_file,
    preview_from_dict,
    preview_to_dict,
)


def _existing() -> list[ExistingEmployeeRecord]:
    return [
        ExistingEmployeeRecord("emp-1", "Samantha Smith", "Samantha", "Smith"),
        ExistingEmployeeRecord("emp-2", "Jordan Patel", "Jordan", "Patel"),
    ]


def test_parse_roster_csv_and_build_preview() -> None:
    csv_text = (
        "Full Name,Role (MLT/MLA),Seniority Hours,Target Weekly Hours\n"
        "Alex Morgan,MLT,4500,40\n"
        "Sam Smith,MLA,,24\n"
    )
    frame = parse_roster_file(content=csv_text.encode("utf-8"), filename="roster.csv")
    preview = build_import_preview(
        frame,
        source_filename="roster.csv",
        existing_employees=_existing(),
    )
    assert preview.insert_count == 1
    assert preview.update_count == 1
    assert preview.needs_seniority_count == 1
    assert preview.rows[1].needs_seniority_manual is True
    assert preview.rows[0].fte == 1.0
    assert preview.rows[1].fte == 0.6


def test_fuzzy_match_sam_to_samantha_smith() -> None:
    match = fuzzy_match_existing_employee("Sam Smith", _existing())
    assert match is not None
    employee, confidence = match
    assert employee.id == "emp-1"
    assert confidence >= 0.72


def test_build_preview_flags_missing_mandatory_fields() -> None:
    csv_text = "Full Name,Role (MLT/MLA),Target Weekly Hours\n,MLT,\n"
    frame = parse_roster_file(content=csv_text.encode("utf-8"), filename="bad.csv")
    preview = build_import_preview(
        frame,
        source_filename="bad.csv",
        existing_employees=[],
    )
    assert preview.error_count == 1
    assert "Full Name is required" in preview.rows[0].validation_errors[0]
    assert preview.can_commit is False


def test_commit_import_preview_bulk_insert() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE employees (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          employee_code TEXT,
          first_name TEXT NOT NULL,
          last_name TEXT NOT NULL,
          hire_date TEXT NOT NULL,
          fte REAL NOT NULL,
          base_hourly_rate REAL NOT NULL DEFAULT 40.0,
          seniority_hours REAL NOT NULL DEFAULT 0.0,
          contract_line_type TEXT,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE employee_qualifications (
          tenant_id TEXT NOT NULL,
          employee_id TEXT NOT NULL,
          qualification_id TEXT NOT NULL,
          awarded_on TEXT,
          expires_on TEXT,
          created_at TEXT NOT NULL,
          PRIMARY KEY (tenant_id, employee_id, qualification_id)
        );
        CREATE TABLE qualifications (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          code TEXT NOT NULL
        );
        INSERT INTO qualifications VALUES ('qual-mlt', 'tenant-a', 'MLT');
        INSERT INTO qualifications VALUES ('qual-mla', 'tenant-a', 'MLA');
        """
    )
    csv_text = (
        "Full Name,Role (MLT/MLA),Seniority Hours,Target Weekly Hours\n"
        "Alex Morgan,MLT,4500,40\n"
    )
    frame = parse_roster_file(content=csv_text.encode("utf-8"), filename="roster.csv")
    preview = build_import_preview(
        frame,
        source_filename="roster.csv",
        existing_employees=[],
    )
    preview.rows[0].seniority_hours = 4500.0
    preview.rows[0].needs_seniority_manual = False

    result = commit_import_preview(
        conn,
        tenant_id="tenant-a",
        preview=preview,
        qualification_ids={"MLT": "qual-mlt", "MLA": "qual-mla"},
    )
    assert result.inserted == 1
    row = conn.execute(
        "SELECT first_name, seniority_hours, fte FROM employees"
    ).fetchone()
    assert row == ("Alex", 4500.0, 1.0)


def test_preview_round_trip_dict() -> None:
    csv_text = (
        "Full Name,Role (MLT/MLA),Seniority Hours,Target Weekly Hours\n"
        "Jordan Patel,MLT,6800,32\n"
    )
    frame = parse_roster_file(content=csv_text.encode("utf-8"), filename="roster.csv")
    preview = build_import_preview(
        frame,
        source_filename="roster.csv",
        existing_employees=_existing(),
    )
    restored = preview_from_dict(preview_to_dict(preview))
    assert restored.update_count == 1
    assert restored.rows[0].matched_existing_id == "emp-2"


def test_parse_portage_rotation_label_from_full_name() -> None:
    csv_text = (
        "Full Name,Seniority Hours\n"
        "MLT 1 (1.0 D/N),4500\n"
        "MLA 2 (0.7 D/E),1200\n"
    )
    frame = parse_roster_file(content=csv_text.encode("utf-8"), filename="portage.csv")
    preview = build_import_preview(
        frame,
        source_filename="portage.csv",
        existing_employees=[],
    )
    assert preview.insert_count == 2
    assert preview.rows[0].role_code == "MLT"
    assert preview.rows[0].fte == 1.0
    assert preview.rows[0].contract_line_type == "D/N"
    assert preview.rows[0].target_weekly_hours == 40.0
    assert preview.rows[1].contract_line_type == "D/E"
    assert preview.rows[1].fte == 0.7


def test_next_employee_code_skips_existing_codes() -> None:
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE employees (
          id TEXT PRIMARY KEY,
          tenant_id TEXT NOT NULL,
          employee_code TEXT,
          first_name TEXT NOT NULL,
          last_name TEXT NOT NULL,
          hire_date TEXT NOT NULL,
          fte REAL NOT NULL,
          is_active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE (tenant_id, employee_code)
        )
        """
    )
    now = "2026-05-26T00:00:00Z"
    for code in ("E1001", "E1002", "E1010"):
        conn.execute(
            """
            INSERT INTO employees (
              id, tenant_id, employee_code, first_name, last_name,
              hire_date, fte, is_active, created_at, updated_at
            ) VALUES (?, 'tenant-a', ?, 'Test', 'User', '2026-01-01', 1.0, 1, ?, ?)
            """,
            (f"emp-{code}", code, now, now),
        )

    from lab_scheduler.data.import_manager import next_employee_code

    assert next_employee_code(conn, "tenant-a") == "E1011"


def test_parse_roster_missing_required_column_raises() -> None:
    csv_text = "Seniority Hours\n4500\n"
    frame = parse_roster_file(content=csv_text.encode("utf-8"), filename="roster.csv")
    with pytest.raises(RosterImportError):
        build_import_preview(
            frame,
            source_filename="roster.csv",
            existing_employees=[],
        )
