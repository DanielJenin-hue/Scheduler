from datetime import date
from pathlib import Path
import json

import pytest

from lab_scheduler.audit.triage_escalation import write_triage_escalation_report
from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.workers.export_worker import ExportWorkerInput, run_export_worker
from lab_scheduler.workers.logic_worker import LogicWorkerOutput, LogicWorkerStatus, TriageEntry


def test_export_worker_writes_breakroom_html_with_triage_tags(tmp_path: Path) -> None:
    triage_output = LogicWorkerOutput(
        status=LogicWorkerStatus.PARTIAL_SUCCESS,
        assignments=(
            {
                "employee_id": "portage-mlt-03",
                "shift_template_id": "shift-morning",
                "assignment_date": "2026-06-01",
            },
        ),
        triage_list=(
            TriageEntry(
                slot_id="slot-1",
                slot="Vacant MLT D/N - Line 03",
                assignment_date=date(2026, 6, 12),
                error_code=ScheduleError.ERR_IMPOSSIBLE_COVERAGE,
                blocked_by=ScheduleError.MAX_WEEKLY_HOURS,
                deficit_hours=8.0,
            ),
        ),
    )
    triage_path = write_triage_escalation_report(
        tmp_path,
        triage_output,
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 28),
        report_date=date(2026, 5, 27),
    )
    triage_relative = triage_path.relative_to(tmp_path).as_posix()

    employees = [
        {
            "id": "portage-mlt-03",
            "full_name": "Vacant MLT D/N - Line 03",
            "fte": 1.0,
            "contract_line_type": "D/N",
        }
    ]
    templates = {
        "shift-morning": {"id": "shift-morning", "code": "MORNING", "short": "MORNING"},
    }

    result = run_export_worker(
        tmp_path,
        ExportWorkerInput(
            # NOTE: the triage entry flags 2026-06-12, which is intentionally left
            # UNASSIGNED here. A triage-escalated slot is an unfilled seat, so the
            # tag should render on the empty cell. (An assigned shift on the same
            # date would win and correctly suppress the tag - they are mutually
            # exclusive states, never co-rendered.)
            assignments=[
                {
                    "employee_id": "portage-mlt-03",
                    "shift_template_id": "shift-morning",
                    "assignment_date": date(2026, 6, 1),
                },
            ],
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 28),
            triage_escalation_path=triage_relative,
            employees=employees,
            shift_templates=templates,
            week_count=4,
        ),
        report_date=date(2026, 5, 27),
    )

    assert result.breakroom_html_path is not None
    assert result.breakroom_html_path.is_file()
    html = result.breakroom_html_path.read_text(encoding="utf-8")
    assert "[UNFILLED - ESCALATED]" in html
    assert "triage-escalated-tag" in html


def test_export_worker_writes_shift_equity_metrics(tmp_path: Path) -> None:
    equity_metrics = {
        "MLT_D_N_Pool": {
            "target_avg_nights": 18,
            "line_01": {"total_D": 22, "total_N": 18, "variance_from_avg": "0"},
        }
    }

    result = run_export_worker(
        tmp_path,
        ExportWorkerInput(
            assignments=[
                {
                    "employee_id": "portage-mlt-01",
                    "shift_template_id": "shift-morning",
                    "assignment_date": date(2026, 6, 1),
                }
            ],
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 7),
            shift_equity_metrics=equity_metrics,
        ),
        report_date=date(2026, 5, 27),
    )

    payload = json.loads(result.export_path.read_text(encoding="utf-8"))
    assert payload["shift_equity_metrics"] == equity_metrics
