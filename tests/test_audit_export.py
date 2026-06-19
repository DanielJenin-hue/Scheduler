from datetime import date

from lab_scheduler.compliance import MANITOBA, ComplianceReport, ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.audit_export import (
    TenantMetadata,
    build_rules_evaluated,
    compile_compliance_audit,
    generate_audit_export_html,
)
from lab_scheduler.scheduling.auto_generate import EmployeeProfile


def _templates() -> dict[str, ShiftTemplateInfo]:
    return {
        "shift-morning": ShiftTemplateInfo(
            "shift-morning", "MORNING", "Morning", "07:00", "15:00", 480, False
        ),
    }


def _tenant() -> TenantMetadata:
    return TenantMetadata(
        id="tenant-northstar-lab",
        name="Northstar Medical Laboratory",
        slug="northstar-lab",
        status="active",
    )


def test_build_rules_evaluated_includes_manitoba_daily_ot() -> None:
    rules = build_rules_evaluated(MANITOBA)
    assert any("Daily overtime threshold: 8 hours" in r for r in rules)
    assert any("Weekly overtime threshold: 40 hours" in r for r in rules)


def test_compile_audit_summary_for_partial_schedule() -> None:
    assignments = [
        ScheduledShift("emp-a1", "Avery Miller", date(2026, 6, 1), "shift-morning"),
    ]
    report = ComplianceReport(jurisdiction_code="MB")
    summary = compile_compliance_audit(
        tenant=_tenant(),
        period_id="period-2026-summer",
        period_name="Summer 2026 Master Rotation",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        week_count=1,
        rules=MANITOBA,
        compliance_report=report,
        assignments=assignments,
        shift_templates=_templates(),
        shift_required_qualifications={"shift-morning": {"qual-mlt"}},
        employees=[
            EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"}),
        ],
        report_id="audit-test-001",
        generated_at=__import__("datetime").datetime(2026, 5, 26, 12, 0, tzinfo=__import__("datetime").timezone.utc),
    )
    assert summary.coverage.is_partial
    assert summary.coverage.is_empty is False
    assert summary.tenant.id == "tenant-northstar-lab"
    assert summary.statute_reference == "Manitoba Employment Standards Code"
    assert len(summary.content_hash) == 64


def test_rendered_html_contains_attestation_and_hash() -> None:
    summary = compile_compliance_audit(
        tenant=_tenant(),
        period_id="period-2026-summer",
        period_name="Summer 2026 Master Rotation",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 7),
        week_count=1,
        rules=MANITOBA,
        compliance_report=ComplianceReport(jurisdiction_code="MB"),
        assignments=[],
        shift_templates=_templates(),
        shift_required_qualifications={"shift-morning": {"qual-mlt"}},
        employees=[EmployeeProfile("emp-a1", "Avery Miller", 1.0, {"qual-mlt"})],
        report_id="audit-test-empty",
        generated_at=__import__("datetime").datetime(2026, 5, 26, 12, 0, tzinfo=__import__("datetime").timezone.utc),
    )
    html_doc = generate_audit_export_html(summary)
    assert "Laboratory Director attestation" in html_doc
    assert summary.content_hash in html_doc
    assert "tenant-northstar-lab" in html_doc
    assert "Manitoba Employment Standards Code" in html_doc
    assert "No shifts scheduled" in html_doc or "empty" in html_doc.lower()
