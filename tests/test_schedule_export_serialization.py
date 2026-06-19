from datetime import date

from lab_scheduler.scheduling.schedule_export import (
    build_schedule_export_rows,
    dedupe_roster_for_schedule_export,
    merge_fragmented_schedule_rows,
    template_record_to_display_token,
)


def test_template_record_maps_morning_to_day_token() -> None:
    assert template_record_to_display_token({"code": "MORNING", "short": "M"}) == "D"
    assert template_record_to_display_token({"code": "EVENING", "short": "E"}) == "E"
    assert template_record_to_display_token({"code": "NIGHT", "short": "N"}) == "N"


def test_build_schedule_export_rows_includes_day_tokens_and_metadata() -> None:
    employees = [
        {
            "id": "portage-mlt-08",
            "full_name": "Vacant MLT D/E - Line 08",
            "fte": 1.0,
            "contract_line_type": "D/E",
        }
    ]
    dates = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    templates = {
        "shift-morning": {"code": "MORNING", "short": "D"},
        "shift-evening": {"code": "EVENING", "short": "E"},
    }
    assignments = [
        {
            "employee_id": "portage-mlt-08",
            "assignment_date": date(2026, 6, 1),
            "shift_template_id": "shift-morning",
        },
        {
            "employee_id": "portage-mlt-08",
            "assignment_date": date(2026, 6, 2),
            "shift_template_id": "shift-evening",
        },
    ]
    rows = build_schedule_export_rows(employees, dates, assignments, templates, include_daily_tallies=False)
    assert len(rows) == 1
    row = rows[0]
    assert row["employee_id"] == "portage-mlt-08"
    assert row["fte"] == 1.0
    assert row["contract_line_type"] == "D/E"
    assert row["2026-06-01"] == "D"
    assert row["2026-06-02"] == "E"


def test_build_schedule_export_rows_marks_forced_clinical_ot() -> None:
    employees = [
        {
            "id": "portage-mlt-08",
            "full_name": "Vacant MLT D/E - Line 08",
            "fte": 1.0,
            "contract_line_type": "D/E",
        }
    ]
    dates = [date(2026, 6, 1)]
    templates = {
        "shift-night": {"code": "NIGHT", "short": "N"},
    }
    assignments = [
        {
            "employee_id": "portage-mlt-08",
            "assignment_date": date(2026, 6, 1),
            "shift_template_id": "shift-night",
            "forced_clinical_ot": True,
        },
    ]
    rows = build_schedule_export_rows(
        employees,
        dates,
        assignments,
        templates,
        include_daily_tallies=False,
    )
    assert rows[0]["2026-06-01"] == "FORCED_CLINICAL_OT"


def test_dedupe_roster_collapses_duplicate_line_rows() -> None:
    employees = [
        {
            "id": "emp-legacy-08",
            "full_name": "Vacant MLT D/E - Line 08",
            "fte": 1.0,
            "contract_line_type": "D/E",
        },
        {
            "id": "portage-mlt-08",
            "full_name": "Vacant MLT D/E - Line 08",
            "fte": 1.0,
            "contract_line_type": "D/E",
        },
    ]
    deduped = dedupe_roster_for_schedule_export(
        employees,
        assignment_counts={"portage-mlt-08": 5, "emp-legacy-08": 0},
    )
    assert len(deduped) == 1
    assert deduped[0]["id"] == "portage-mlt-08"


def test_merge_fragmented_schedule_rows_combines_shift_spillover() -> None:
    dates = [date(2026, 6, 3), date(2026, 6, 4)]
    fragmented = [
        {
            "Employee": "Vacant MLT D/E - Line 08",
            "employee_id": "E",
            "fte": "—",
            "contract_line_type": "—",
            "2026-06-03": "—",
            "2026-06-04": "—",
        },
        {
            "Employee": "Vacant MLT D/E - Line 08",
            "employee_id": "portage-mlt-08",
            "fte": 1.0,
            "contract_line_type": "D/E",
            "2026-06-03": "E",
            "2026-06-04": "E",
        },
    ]
    merged = merge_fragmented_schedule_rows(fragmented, dates)
    assert len(merged) == 1
    row = merged[0]
    assert row["employee_id"] == "portage-mlt-08"
    assert row["contract_line_type"] == "D/E"
    assert row["2026-06-03"] == "E"
