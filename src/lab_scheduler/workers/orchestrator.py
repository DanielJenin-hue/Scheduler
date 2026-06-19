from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from lab_scheduler.audit.triage_escalation import (
    relative_export_path,
    write_triage_escalation_report,
)
from lab_scheduler.paths import resolve_project_path
from lab_scheduler.scheduling.agency_worker import AgencyHandoffResult, run_agency_worker
from lab_scheduler.workers.export_worker import ExportWorkerInput, ExportWorkerResult, run_export_worker
from lab_scheduler.workers.logic_worker import LogicWorkerOutput, LogicWorkerStatus


@dataclass(frozen=True, slots=True)
class OrchestratorPipelineResult:
    """Result of orchestrator task routing after a logic-worker response."""

    halted: bool
    logic_status: LogicWorkerStatus
    triage_escalation_path: Optional[str]
    export_result: Optional[ExportWorkerResult]
    agency_result: Optional[AgencyHandoffResult] = None
    halt_reason: Optional[str] = None


ExportWorkerRunner = Callable[[Path, ExportWorkerInput], ExportWorkerResult]


def route_logic_worker_output(
    project_root: Path,
    logic_output: LogicWorkerOutput,
    *,
    period_start: date,
    period_end: date,
    report_date: Optional[date] = None,
    export_worker: ExportWorkerRunner = run_export_worker,
    export_input: Optional[ExportWorkerInput] = None,
    tenant_id: Optional[str] = None,
    schedule_period_id: Optional[str] = None,
) -> OrchestratorPipelineResult:
    """
    Route logic-worker output through the orchestrator pipeline.

    ``PARTIAL_SUCCESS`` with a populated ``triage_list`` writes
    ``exports/Triage_Escalation_[DATE].json`` and continues to the export worker.
    """

    if logic_output.status is LogicWorkerStatus.FAILURE:
        return OrchestratorPipelineResult(
            halted=True,
            logic_status=logic_output.status,
            triage_escalation_path=None,
            export_result=None,
            halt_reason="Logic worker returned FAILURE with no assignable block.",
        )

    triage_path: Optional[Path] = None
    if (
        logic_output.status is LogicWorkerStatus.PARTIAL_SUCCESS
        and logic_output.triage_list
    ):
        triage_path = write_triage_escalation_report(
            project_root,
            logic_output,
            period_start=period_start,
            period_end=period_end,
            report_date=report_date,
        )

    triage_relative = (
        relative_export_path(project_root, triage_path) if triage_path is not None else None
    )
    if (
        logic_output.status is LogicWorkerStatus.PARTIAL_SUCCESS
        and logic_output.triage_list
        and triage_relative is None
    ):
        raise RuntimeError(
            "PARTIAL_SUCCESS run requires a persisted Triage_Escalation JSON path "
            "but none was produced."
        )

    base_input = export_input or ExportWorkerInput(
        assignments=logic_output.assignments,
        period_start=period_start,
        period_end=period_end,
    )
    worker_input = ExportWorkerInput(
        assignments=base_input.assignments,
        period_start=base_input.period_start,
        period_end=base_input.period_end,
        triage_escalation_path=triage_relative,
        employees=base_input.employees,
        shift_templates=base_input.shift_templates,
        week_count=base_input.week_count,
        facility_name=base_input.facility_name,
        period_name=base_input.period_name,
        render_breakroom_html=base_input.render_breakroom_html,
        shift_equity_metrics=dict(logic_output.shift_equity_metrics or {}),
        staff_fairness_report=dict(base_input.staff_fairness_report or {}),
        staff_fairness_html=base_input.staff_fairness_html,
        schedule_archetype=logic_output.schedule_archetype,
    )

    export_result = export_worker(project_root, worker_input, report_date=report_date)

    agency_result: Optional[AgencyHandoffResult] = None
    if triage_relative and export_result.breakroom_html_path is not None:
        triage_absolute = resolve_project_path(project_root, triage_relative)
        agency_result = run_agency_worker(
            project_root,
            triage_absolute,
            report_date=report_date,
            facility_name=base_input.facility_name,
            period_start=period_start,
            period_end=period_end,
            tenant_id=tenant_id,
            schedule_period_id=schedule_period_id,
        )

    return OrchestratorPipelineResult(
        halted=False,
        logic_status=logic_output.status,
        triage_escalation_path=triage_relative,
        export_result=export_result,
        agency_result=agency_result,
    )
