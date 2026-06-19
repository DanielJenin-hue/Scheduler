from __future__ import annotations

from datetime import date
from pathlib import Path

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.rsi.clinical_audit import (
    build_seat_fill_counts,
    detect_clinical_floor_breaches,
    expand_portage_slots,
    operational_reliability_pct,
)
from lab_scheduler.rsi.manager import RSIAutoManager
from lab_scheduler.rsi.prospector import build_viability_report, run_prospector_scan
from lab_scheduler.rsi.project_health import compute_total_mrr
from lab_scheduler.rsi.self_correction import build_risk_mitigation_report
from lab_scheduler.rsi.value_dashboard import build_value_first_dashboard
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.simulation.hospital_stress import QUAL_MLT, shift_templates


def test_project_health_mrr_calculation() -> None:
    assert compute_total_mrr(3) == 897.0


def test_prospector_builds_viability_reports(tmp_path: Path) -> None:
    dataset = tmp_path / "facilities.csv"
    dataset.write_text(
        "facility_id,facility_name,region,state_province,annual_test_volume,mlt_fte,mla_fte\n"
        "X1,Metro Lab,Ontario,ON,900000,10,8\n"
        "X2,Small Lab,Ontario,ON,100000,2,1\n",
        encoding="utf-8",
    )
    reports = run_prospector_scan(dataset, high_volume_threshold=750_000)
    assert len(reports) == 1
    assert reports[0].facility_id == "X1"
    assert reports[0].estimated_annual_savings_usd > 0


def test_rsi_initialize_and_dashboard(tmp_path: Path) -> None:
    manager = RSIAutoManager(project_root=tmp_path, rules=MANITOBA)
    manager.initialize()
    assert manager.manifest_path.is_file()
    assert manager.dashboard_path.is_file()


def test_clinical_floor_breach_detection() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 3)
    templates = shift_templates()
    night_id = next(tid for tid, tmpl in templates.items() if tmpl.code == "NIGHT")
    employees = [
        EmployeeProfile("emp-mlt", "MLT Tech", 1.0, {QUAL_MLT}, contract_line_type="D/N"),
    ]
    assignments = [
        {
            "employee_id": "emp-mlt",
            "shift_template_id": night_id,
            "assignment_date": period_start,
            "forced_clinical_ot": False,
        }
    ]
    expanded = expand_portage_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=templates,
    )
    fill_counts = build_seat_fill_counts(assignments, employees)
    breaches = detect_clinical_floor_breaches(
        fill_counts=fill_counts,
        shift_templates=templates,
        period_start=period_start,
        period_end=period_end,
        expanded_slots=expanded,
    )
    assert breaches
    reliability = operational_reliability_pct(
        period_start=period_start,
        period_end=period_end,
        breaches=breaches,
        forced_ot_count=0,
    )
    assert reliability < 100.0


def test_risk_mitigation_report_shape() -> None:
    from lab_scheduler.rsi.clinical_audit import ClinicalFloorBreach

    breach = ClinicalFloorBreach(
        assignment_date=date(2026, 6, 1),
        shift_code="NIGHT",
        required_seats=2,
        filled_seats=1,
    )
    report = build_risk_mitigation_report(
        report_date=date(2026, 6, 2),
        breaches=[breach],
        forced_ot_count=0,
        proposed_swaps=[],
    )
    payload = report.to_dict()
    assert payload["breach_count"] == 1
    assert payload["breaches"][0]["shift_code"] == "NIGHT"


def test_value_first_dashboard_three_metrics() -> None:
    from lab_scheduler.rsi.prospector import RegionalFacilityRecord

    report = build_viability_report(
        RegionalFacilityRecord(
            facility_id="A1",
            facility_name="Alpha Lab",
            region="Prairies",
            state_province="MB",
            annual_test_volume=900_000,
            mlt_fte=10.0,
            mla_fte=8.0,
        )
    )
    dashboard = build_value_first_dashboard(
        updated_on=date(2026, 6, 1),
        operational_reliability_pct=98.5,
        total_revenue_month_usd=897.0,
        prospect_reports=[report],
    )
    payload = dashboard.to_dict()
    assert set(payload.keys()) == {
        "updated_on",
        "operational_reliability_pct",
        "total_revenue_month_usd",
        "next_best_facility_target",
    }
    assert payload["next_best_facility_target"] is not None
