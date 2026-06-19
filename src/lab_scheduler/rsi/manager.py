from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import List, Optional, Sequence

import sqlite3

from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.rsi.clinical_audit import (
    breaches_to_risk_instances,
    build_seat_fill_counts,
    detect_clinical_floor_breaches,
    detect_forced_clinical_ot,
    expand_portage_slots,
    operational_reliability_pct,
)
from lab_scheduler.rsi.db_context import (
    ScheduleAuditContext,
    assignments_to_scheduled,
    count_active_tenants,
    load_schedule_audit_context,
)
from lab_scheduler.rsi.project_health import (
    ProjectHealthManifest,
    compute_total_mrr,
    merge_clinical_risks,
)
from lab_scheduler.rsi.prospector import ViabilityReport, run_prospector_scan
from lab_scheduler.rsi.self_correction import (
    RiskMitigationReport,
    build_risk_mitigation_report,
    propose_shift_swaps_for_breaches,
)
from lab_scheduler.rsi.storage import ensure_rsi_layout, read_json, utc_now_iso, write_json
from lab_scheduler.rsi.value_dashboard import ValueFirstDashboard, build_value_first_dashboard


@dataclass(frozen=True, slots=True)
class RSICycleResult:
    project_health: ProjectHealthManifest
    risk_report: Optional[RiskMitigationReport]
    dashboard: ValueFirstDashboard
    viability_reports: Sequence[ViabilityReport]
    prospector_ran: bool


class RSIAutoManager:
    """
    Recursive Strategic Infrastructure (RSI) orchestrator.

    Runs the daily self-correction loop and weekly Prospector scan, then refreshes
    the Value-First executive dashboard.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        rules: JurisdictionRules,
        facility_dataset_path: Optional[Path] = None,
    ) -> None:
        self.project_root = project_root
        self.rules = rules
        self.rsi_root = ensure_rsi_layout(project_root)
        default_dataset = project_root / "data" / "rsi" / "regional_facilities.csv"
        self.facility_dataset_path = facility_dataset_path or default_dataset
        self._last_prospector_run: Optional[date] = None

    @property
    def manifest_path(self) -> Path:
        return self.rsi_root / "manifests" / "project_health.json"

    @property
    def dashboard_path(self) -> Path:
        return self.rsi_root / "dashboard" / "value_first.json"

    @property
    def reports_dir(self) -> Path:
        return self.rsi_root / "reports"

    @property
    def prospector_dir(self) -> Path:
        return self.rsi_root / "prospector"

    def initialize(self) -> Path:
        """Bootstrap RSI storage and seed an empty Project Health manifest."""
        manifest = ProjectHealthManifest(updated_at=utc_now_iso())
        write_json(self.manifest_path, manifest.to_dict())
        dashboard = ValueFirstDashboard(
            updated_on=date.today(),
            operational_reliability_pct=100.0,
            total_revenue_month_usd=0.0,
            next_best_facility_target=None,
        )
        write_json(self.dashboard_path, dashboard.to_dict())
        return self.rsi_root

    def load_manifest(self) -> ProjectHealthManifest:
        payload = read_json(self.manifest_path)
        if payload is None:
            return ProjectHealthManifest(updated_at=utc_now_iso())
        return ProjectHealthManifest.from_dict(payload)

    def load_dashboard(self) -> Optional[ValueFirstDashboard]:
        payload = read_json(self.dashboard_path)
        if payload is None:
            return None
        return ValueFirstDashboard.from_dict(payload)

    def _should_run_prospector(self, today: date, *, force_weekly: bool) -> bool:
        if force_weekly:
            return True
        if self._last_prospector_run == today:
            return False
        marker = self.prospector_dir / "last_weekly_run.json"
        payload = read_json(marker)
        if payload is None:
            return today.weekday() == 0
        last_run = date.fromisoformat(str(payload.get("last_run_date", today.isoformat())))
        return (today - last_run).days >= 7

    def run_daily_cycle(
        self,
        conn: sqlite3.Connection,
        *,
        audit_context: ScheduleAuditContext,
        today: Optional[date] = None,
        force_prospector: bool = False,
    ) -> RSICycleResult:
        """Daily audit: clinical floor, self-correction report, dashboard refresh."""

        today = today or date.today()
        expanded_slots = expand_portage_slots(
            period_start=audit_context.period_start,
            period_end=audit_context.period_end,
            shift_templates=audit_context.shift_templates,
        )
        fill_counts = build_seat_fill_counts(
            audit_context.assignments,
            audit_context.employees,
            qual_codes=audit_context.qual_codes,
        )

        forced_risks = detect_forced_clinical_ot(
            audit_context.assignments,
            audit_context.shift_templates,
        )
        breaches = detect_clinical_floor_breaches(
            fill_counts=fill_counts,
            shift_templates=audit_context.shift_templates,
            period_start=audit_context.period_start,
            period_end=audit_context.period_end,
            expanded_slots=expanded_slots,
        )
        breach_risks = breaches_to_risk_instances(breaches)
        incoming_risks = forced_risks + breach_risks

        prior = self.load_manifest()
        active_tenants = count_active_tenants(conn)
        manifest = ProjectHealthManifest(
            updated_at=utc_now_iso(),
            total_mrr_usd=compute_total_mrr(active_tenants),
            active_tenant_count=active_tenants,
            clinical_risk_instances=merge_clinical_risks(
                prior.clinical_risk_instances,
                incoming_risks,
            ),
        )
        write_json(self.manifest_path, manifest.to_dict())

        risk_report: Optional[RiskMitigationReport] = None
        if breaches:
            scheduled = assignments_to_scheduled(
                audit_context.assignments,
                [{"id": employee.id, "full_name": employee.full_name} for employee in audit_context.employees],
            )
            swaps = propose_shift_swaps_for_breaches(
                breaches=breaches,
                rules=self.rules,
                period_start=audit_context.period_start,
                period_end=audit_context.period_end,
                weeks_in_period=audit_context.weeks_in_period,
                employees=audit_context.employees,
                assignments=scheduled,
                shift_templates=dict(audit_context.shift_templates),
                shift_required_qualifications=dict(audit_context.shift_required_qualifications),
                employee_target_hours=audit_context.employee_target_hours,
            )
            risk_report = build_risk_mitigation_report(
                report_date=today,
                breaches=breaches,
                forced_ot_count=len(forced_risks),
                proposed_swaps=swaps,
            )
            report_path = self.reports_dir / f"risk_mitigation_{today.isoformat()}.json"
            write_json(report_path, risk_report.to_dict())

        viability_reports: List[ViabilityReport] = []
        prospector_ran = False
        if self._should_run_prospector(today, force_weekly=force_prospector):
            viability_reports = run_prospector_scan(self.facility_dataset_path)
            prospector_ran = True
            self._last_prospector_run = today
            write_json(
                self.prospector_dir / f"viability_{today.isoformat()}.json",
                {"generated_on": today.isoformat(), "reports": [report.to_dict() for report in viability_reports]},
            )
            write_json(
                self.prospector_dir / "last_weekly_run.json",
                {"last_run_date": today.isoformat()},
            )
        else:
            latest = sorted(self.prospector_dir.glob("viability_*.json"))
            if latest:
                payload = read_json(latest[-1])
                if payload:
                    viability_reports = [
                        ViabilityReport(
                            facility_id=str(item["facility_id"]),
                            facility_name=str(item["facility_name"]),
                            region=str(item["region"]),
                            annual_test_volume=int(item["annual_test_volume"]),
                            estimated_annual_savings_usd=float(item["estimated_annual_savings_usd"]),
                            deployment_score=float(item["deployment_score"]),
                            rationale=str(item["rationale"]),
                        )
                        for item in payload.get("reports", [])
                    ]

        reliability = operational_reliability_pct(
            period_start=audit_context.period_start,
            period_end=audit_context.period_end,
            breaches=breaches,
            forced_ot_count=len(forced_risks),
        )
        dashboard = build_value_first_dashboard(
            updated_on=today,
            operational_reliability_pct=reliability,
            total_revenue_month_usd=manifest.total_mrr_usd,
            prospect_reports=viability_reports,
        )
        write_json(self.dashboard_path, dashboard.to_dict())

        return RSICycleResult(
            project_health=manifest,
            risk_report=risk_report,
            dashboard=dashboard,
            viability_reports=viability_reports,
            prospector_ran=prospector_ran,
        )

    def run_self_correction_loop(
        self,
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        period_id: str,
        employees: List[dict],
        templates: dict,
        assignments: List[dict],
        emp_quals: dict,
        target_hours: dict,
        qual_code_map: dict,
        today: Optional[date] = None,
        force_prospector: bool = False,
    ) -> RSICycleResult:
        audit_context = load_schedule_audit_context(
            conn,
            tenant_id=tenant_id,
            period_id=period_id,
            employees=employees,
            templates=templates,
            assignments=assignments,
            emp_quals=emp_quals,
            target_hours=target_hours,
            qual_code_map=qual_code_map,
        )
        return self.run_daily_cycle(
            conn,
            audit_context=audit_context,
            today=today,
            force_prospector=force_prospector,
        )
