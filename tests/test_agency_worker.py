from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.scheduling.agency_worker import (
    STATUS_SENT,
    mark_agency_request_sent,
    run_agency_worker,
)


def _write_triage(tmp_path: Path, triage_list: list[dict]) -> Path:
    path = tmp_path / "exports" / "Triage_Escalation_2026-05-27.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "period_start": "2026-06-01",
                "period_end": "2026-06-28",
                "status": "PARTIAL_SUCCESS",
                "triage_list": triage_list,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def test_agency_worker_groups_impossible_coverage_rows(tmp_path: Path) -> None:
    triage_path = _write_triage(
        tmp_path,
        [
            {
                "slot": "Vacant MLT D/E - Line 05",
                "slot_id": "2026-06-03|MORNING|shift-morning|Weekday Morning - MLT|seat=4|qual=MLT",
                "date": "2026-06-03",
                "shift_code": "MORNING",
                "blocked_by": ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
                "deficit_hours": 8.0,
            },
            {
                "slot": "Vacant MLT D/E - Line 06",
                "slot_id": "2026-06-03|MORNING|shift-morning|Weekday Morning - MLT|seat=5|qual=MLT",
                "date": "2026-06-03",
                "shift_code": "MORNING",
                "blocked_by": ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
                "deficit_hours": 8.0,
            },
            {
                "slot": "Vacant MLA D/E - Line 05",
                "slot_id": "2026-06-05|MORNING|shift-morning|Weekday Morning - MLA|seat=4|qual=MLA",
                "date": "2026-06-05",
                "shift_code": "MORNING",
                "blocked_by": ScheduleError.MAX_WEEKLY_HOURS.value,
                "deficit_hours": 8.0,
            },
        ],
    )

    result = run_agency_worker(
        tmp_path,
        triage_path,
        report_date=date(2026, 5, 27),
        facility_name="Northstar Medical Laboratory",
        schedule_period_id="period-2026-summer",
    )

    assert result.status == "DRAFT_PENDING_APPROVAL"
    assert result.line_item_count == 1
    assert result.escalated_slot_count == 2
    assert result.request_path is not None
    assert result.email_draft_path is not None
    assert result.request_path.is_file()
    assert result.email_draft_path.is_file()

    payload = json.loads(result.request_path.read_text(encoding="utf-8"))
    assert payload["status"] == "DRAFT_PENDING_APPROVAL"
    assert payload["schedule_period_id"] == "period-2026-summer"
    assert len(payload["line_items"]) == 1
    line = payload["line_items"][0]
    assert line["date"] == "2026-06-03"
    assert line["shift_code"] == "MORNING"
    assert line["required_qual"] == "MLT"
    assert line["headcount"] == 2
    assert "Vacant MLT D/E - Line 05" in line["slots"]

    draft = result.email_draft_path.read_text(encoding="utf-8")
    assert "Locum Tenens Request" in draft
    assert "2026-06-03" in draft
    assert "2 shift(s)" in draft
    assert "Northstar Medical Laboratory" in draft


def test_mark_agency_request_sent_updates_status(tmp_path: Path) -> None:
    triage_path = _write_triage(
        tmp_path,
        [
            {
                "slot": "Vacant MLT D/E - Line 05",
                "date": "2026-06-03",
                "shift_code": "MORNING",
                "blocked_by": ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value,
            }
        ],
    )
    result = run_agency_worker(tmp_path, triage_path, report_date=date(2026, 5, 27))
    assert result.request_path is not None

    from lab_scheduler.scheduling.agency_worker import mark_agency_request_sent

    updated = mark_agency_request_sent(tmp_path, result.request_path)
    assert updated["status"] == STATUS_SENT
    assert updated.get("sent_at_utc")
    reread = json.loads(result.request_path.read_text(encoding="utf-8"))
    assert reread["status"] == "SENT"


def test_agency_worker_skips_when_no_impossible_rows(tmp_path: Path) -> None:
    triage_path = _write_triage(
        tmp_path,
        [
            {
                "slot": "Vacant MLA D/E - Line 01",
                "date": "2026-06-03",
                "shift_code": "EVENING",
                "blocked_by": ScheduleError.MAX_WEEKLY_HOURS.value,
            }
        ],
    )

    result = run_agency_worker(tmp_path, triage_path, report_date=date(2026, 5, 27))

    assert result.status == "SKIPPED_NO_ESCALATIONS"
    assert result.request_path is None
