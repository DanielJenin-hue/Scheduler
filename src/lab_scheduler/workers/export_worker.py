from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from lab_scheduler.paths import resolve_project_path

logger = logging.getLogger(__name__)

SCHEDULE_EXPORT_PREFIX = "Schedule_Export"
BREAKROOM_EXPORT_PREFIX = "breakroom_schedule"
STAFF_FAIRNESS_EXPORT_PREFIX = "staff_fairness"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class ExportWorkerInput:
    """Downstream payload from the orchestrator after logic-worker routing."""

    assignments: Sequence[Mapping[str, object]]
    period_start: date
    period_end: date
    triage_escalation_path: Optional[str] = None
    employees: Optional[Sequence[Mapping[str, object]]] = None
    shift_templates: Optional[Mapping[str, Mapping[str, object]]] = None
    week_count: int = 4
    facility_name: str = "Northstar Medical Laboratory"
    period_name: str = "Schedule Block"
    render_breakroom_html: bool = True
    shift_equity_metrics: Optional[Mapping[str, object]] = None
    staff_fairness_report: Optional[Mapping[str, object]] = None
    staff_fairness_html: Optional[str] = None
    schedule_archetype: str = "STANDARD"


@dataclass(frozen=True, slots=True)
class ExportWorkerResult:
    export_path: Path
    triage_escalation_path: Optional[str]
    assignment_count: int
    schedule_row_count: int
    breakroom_html_path: Optional[Path] = None
    staff_fairness_html_path: Optional[Path] = None


def schedule_export_path(project_root: Path, report_date: Optional[date] = None) -> Path:
    stamp = (report_date or date.today()).isoformat()
    return project_root / "exports" / f"{SCHEDULE_EXPORT_PREFIX}_{stamp}.json"


def breakroom_export_path(project_root: Path, report_date: Optional[date] = None) -> Path:
    stamp = (report_date or date.today()).isoformat()
    return project_root / "exports" / f"{BREAKROOM_EXPORT_PREFIX}_{stamp}.html"


def staff_fairness_export_path(project_root: Path, report_date: Optional[date] = None) -> Path:
    stamp = (report_date or date.today()).isoformat()
    return project_root / "exports" / f"{STAFF_FAIRNESS_EXPORT_PREFIX}_{stamp}.html"


def _normalize_assignment_rows(
    assignments: Sequence[Mapping[str, object]],
) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for assignment in assignments:
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        rows.append(
            {
                "employee_id": assignment["employee_id"],
                "shift_template_id": assignment["shift_template_id"],
                "assignment_date": assignment_date.isoformat(),
            }
        )
    return rows


def run_export_worker(
    project_root: Path,
    payload: ExportWorkerInput,
    *,
    report_date: Optional[date] = None,
) -> ExportWorkerResult:
    """
    Write schedule export JSON and render breakroom HTML from provided state.

    This worker does not run generation, compliance, or constraint checks.
    When ``triage_escalation_path`` is set, the triage JSON is ingested purely
    for visual tagging in the HTML grid.
    """

    path = schedule_export_path(project_root, report_date=report_date)
    path.parent.mkdir(parents=True, exist_ok=True)

    assignment_rows = _normalize_assignment_rows(payload.assignments)
    dates = [
        payload.period_start + timedelta(days=offset)
        for offset in range((payload.period_end - payload.period_start).days + 1)
    ]
    schedule_rows: List[Dict[str, object]] = []
    if payload.employees and payload.shift_templates:
        from lab_scheduler.scheduling.schedule_export import build_schedule_export_rows

        normalized_assignments = [
            {
                "employee_id": row["employee_id"],
                "shift_template_id": row["shift_template_id"],
                "assignment_date": date.fromisoformat(str(row["assignment_date"])),
            }
            for row in assignment_rows
        ]
        schedule_rows = build_schedule_export_rows(
            payload.employees,
            dates,
            normalized_assignments,
            payload.shift_templates,
        )

    triage_file: Optional[Path] = None
    if payload.triage_escalation_path:
        triage_file = resolve_project_path(project_root, payload.triage_escalation_path)
        if not triage_file.is_file():
            logger.error(
                "Triage escalation file missing on disk: path=%s resolved=%s project_root=%s",
                payload.triage_escalation_path,
                triage_file,
                project_root.resolve(),
            )

    tagged_rows = schedule_rows
    breakroom_html_path: Optional[Path] = None
    if payload.render_breakroom_html and payload.employees and schedule_rows:
        from lab_scheduler.scheduling.schedule_export import render_breakroom_schedule_html

        tagged_rows, html = render_breakroom_schedule_html(
            schedule_rows=schedule_rows,
            employees=payload.employees,
            dates=dates,
            period_start=payload.period_start,
            period_end=payload.period_end,
            week_count=payload.week_count,
            triage_escalation_path=triage_file,
            facility_name=payload.facility_name,
            period_name=payload.period_name,
            compliance_verified_on=report_date or date.today(),
            schedule_archetype=payload.schedule_archetype,
        )
        breakroom_html_path = breakroom_export_path(project_root, report_date=report_date)
        breakroom_html_path.write_text(html, encoding="utf-8")

    staff_fairness_html_path: Optional[Path] = None
    if payload.staff_fairness_html:
        staff_fairness_html_path = staff_fairness_export_path(project_root, report_date=report_date)
        staff_fairness_html_path.write_text(payload.staff_fairness_html, encoding="utf-8")

    export_payload = {
        "generated_at_utc": _utc_now_iso(),
        "period_start": payload.period_start.isoformat(),
        "period_end": payload.period_end.isoformat(),
        "assignment_count": len(assignment_rows),
        "assignments": assignment_rows,
        "schedule_rows": tagged_rows,
        "triage_escalation_path": payload.triage_escalation_path,
        "breakroom_html_path": (
            breakroom_html_path.relative_to(project_root).as_posix()
            if breakroom_html_path is not None
            else None
        ),
        "staff_fairness_html_path": (
            staff_fairness_html_path.relative_to(project_root).as_posix()
            if staff_fairness_html_path is not None
            else None
        ),
        "shift_equity_metrics": dict(payload.shift_equity_metrics or {}),
        "staff_fairness_report": dict(payload.staff_fairness_report or {}),
    }
    path.write_text(json.dumps(export_payload, indent=2, sort_keys=True), encoding="utf-8")
    return ExportWorkerResult(
        export_path=path,
        triage_escalation_path=payload.triage_escalation_path,
        assignment_count=len(assignment_rows),
        schedule_row_count=len(tagged_rows),
        breakroom_html_path=breakroom_html_path,
        staff_fairness_html_path=staff_fairness_html_path,
    )
