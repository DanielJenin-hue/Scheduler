from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from lab_scheduler.scheduling.fairness_thresholds import (
    DEFAULT_FAIRNESS_THRESHOLDS,
    FairnessThresholds,
)
from lab_scheduler.simulation.hospital_stress import QUAL_MLA, shift_templates as build_shift_templates
from lab_scheduler.validation.staff_fairness_report import (
    STATUS_NOT_RECOMMENDED,
    STATUS_READY,
    STATUS_REVIEW_REQUIRED,
    build_staff_fairness_report,
    generate_staff_fairness_report,
    record_staff_fairness_attestation,
    staff_fairness_export_allowed,
)
from lab_scheduler.workers.export_worker import ExportWorkerInput, run_export_worker


def _template_dict() -> dict[str, dict[str, object]]:
    return {
        tid: {
            "id": tid,
            "code": info.code,
            "name": info.name,
            "start_time": info.start_time,
            "end_time": info.end_time,
            "duration_minutes": info.duration_minutes,
            "crosses_midnight": info.crosses_midnight,
        }
        for tid, info in build_shift_templates().items()
    }


def _employees() -> list[dict[str, object]]:
    return [
        {
            "id": "portage-mla-01",
            "full_name": "Vacant MLA D/E - Line 01",
            "fte": 1.0,
            "contract_line_type": "D/E",
        },
        {
            "id": "portage-mla-02",
            "full_name": "Vacant MLA D/E - Line 02",
            "fte": 1.0,
            "contract_line_type": "D/E",
        },
        {
            "id": "named-mlt",
            "full_name": "Jordan Smith MLT",
            "fte": 1.0,
            "contract_line_type": "D/N",
        },
    ]


@pytest.mark.legacy
def test_build_staff_fairness_report_ready_when_balanced() -> None:
    start = date(2026, 6, 1)
    assignments = [
        {
            "employee_id": "portage-mla-01",
            "shift_template_id": "shift-morning",
            "assignment_date": start,
        },
        {
            "employee_id": "portage-mla-02",
            "shift_template_id": "shift-morning",
            "assignment_date": start + timedelta(days=1),
        },
    ]
    report = build_staff_fairness_report(
        tenant_name="Northstar",
        period_name="Summer 2026",
        period_start=start,
        period_end=start + timedelta(days=13),
        employees=_employees(),
        assignments=assignments,
        shift_templates=_template_dict(),
        target_hours={
            "portage-mla-01": 8.0,
            "portage-mla-02": 8.0,
            "named-mlt": 8.0,
        },
        qual_lookup={
            "portage-mla-01": QUAL_MLA,
            "portage-mla-02": QUAL_MLA,
            "named-mlt": "MLT",
        },
    )
    assert report.overall_status == STATUS_READY
    assert report.attestation_required is False
    assert report.red_flag_count == 0


def test_work_streak_violation_is_red_and_requires_attestation() -> None:
    start = date(2026, 6, 1)
    assignments = [
        {
            "employee_id": "portage-mla-01",
            "shift_template_id": "shift-morning",
            "assignment_date": start + timedelta(days=offset),
        }
        for offset in range(7)
    ]
    report = build_staff_fairness_report(
        tenant_name="Northstar",
        period_name="Summer 2026",
        period_start=start,
        period_end=start + timedelta(days=13),
        employees=_employees(),
        assignments=assignments,
        shift_templates=_template_dict(),
        target_hours={"portage-mla-01": 320.0},
        qual_lookup={"portage-mla-01": QUAL_MLA},
    )
    assert report.overall_status == STATUS_NOT_RECOMMENDED
    assert report.attestation_required is True
    assert any(flag.code == "WORK_STREAK" for flag in report.flags)


def test_d_to_n_transition_is_red() -> None:
    start = date(2026, 6, 1)
    assignments = [
        {
            "employee_id": "named-mlt",
            "shift_template_id": "shift-morning",
            "assignment_date": start,
        },
        {
            "employee_id": "named-mlt",
            "shift_template_id": "shift-night",
            "assignment_date": start + timedelta(days=1),
        },
    ]
    report = build_staff_fairness_report(
        tenant_name="Northstar",
        period_name="Summer 2026",
        period_start=start,
        period_end=start + timedelta(days=13),
        employees=_employees(),
        assignments=assignments,
        shift_templates=_template_dict(),
        target_hours={"named-mlt": 320.0},
        qual_lookup={"named-mlt": "MLT"},
    )
    assert any(flag.code == "D_TO_N_TRANSITION" for flag in report.flags)
    assert report.overall_status == STATUS_NOT_RECOMMENDED


def test_alt_shift_equity_yellow_when_peer_delta_exceeds_one() -> None:
    start = date(2026, 6, 1)
    assignments: list[dict[str, object]] = []
    for offset in (0, 2, 4, 6, 8, 10):
        assignments.append(
            {
                "employee_id": "portage-mla-01",
                "shift_template_id": "shift-evening",
                "assignment_date": start + timedelta(days=offset),
            }
        )
    for offset in (20, 22):
        assignments.append(
            {
                "employee_id": "portage-mla-02",
                "shift_template_id": "shift-evening",
                "assignment_date": start + timedelta(days=offset),
            }
        )
    for offset in (10, 12, 14, 16, 18, 24):
        assignments.append(
            {
                "employee_id": "portage-mla-02",
                "shift_template_id": "shift-morning",
                "assignment_date": start + timedelta(days=offset),
            }
        )
    report = build_staff_fairness_report(
        tenant_name="Northstar",
        period_name="Summer 2026",
        period_start=start,
        period_end=start + timedelta(days=27),
        employees=_employees()[:2],
        assignments=assignments,
        shift_templates=_template_dict(),
        target_hours={
            "portage-mla-01": 320.0,
            "portage-mla-02": 320.0,
        },
        qual_lookup={
            "portage-mla-01": QUAL_MLA,
            "portage-mla-02": QUAL_MLA,
        },
    )
    assert any(flag.code == "ALT_SHIFT_EQUITY" for flag in report.flags)
    assert report.overall_status == STATUS_REVIEW_REQUIRED


def test_staff_fairness_export_always_allowed() -> None:
    report = build_staff_fairness_report(
        tenant_name="Northstar",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 14),
        employees=_employees()[:1],
        assignments=[
            {
                "employee_id": "portage-mla-01",
                "shift_template_id": "shift-morning",
                "assignment_date": date(2026, 6, 1) + timedelta(days=offset),
            }
            for offset in range(7)
        ],
        shift_templates=_template_dict(),
        target_hours={"portage-mla-01": 320.0},
        qual_lookup={"portage-mla-01": QUAL_MLA},
    )
    assert report.attestation_required
    assert staff_fairness_export_allowed(report, attested=False) is True
    assert staff_fairness_export_allowed(report, attested=True) is True
    assert staff_fairness_export_allowed(report.to_dict(), attested=False) is True


def test_render_html_includes_employee_and_flag_codes() -> None:
    report, html_doc = generate_staff_fairness_report(
        tenant_name="Northstar",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 14),
        employees=_employees()[:1],
        assignments=[
            {
                "employee_id": "portage-mla-01",
                "shift_template_id": "shift-morning",
                "assignment_date": date(2026, 6, 1) + timedelta(days=offset),
            }
            for offset in range(7)
        ],
        shift_templates=_template_dict(),
        target_hours={"portage-mla-01": 320.0},
        qual_lookup={"portage-mla-01": QUAL_MLA},
    )
    assert "Vacant MLA D/E - Line 01" in html_doc
    assert "WORK_STREAK" in html_doc
    assert report.report_id in html_doc


def test_export_worker_persists_staff_fairness_report(tmp_path: Path) -> None:
    report, html_doc = generate_staff_fairness_report(
        tenant_name="Northstar",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 14),
        employees=_employees()[:1],
        assignments=[
            {
                "employee_id": "portage-mla-01",
                "shift_template_id": "shift-morning",
                "assignment_date": date(2026, 6, 1),
            }
        ],
        shift_templates=_template_dict(),
        target_hours={"portage-mla-01": 320.0},
        qual_lookup={"portage-mla-01": QUAL_MLA},
    )
    result = run_export_worker(
        tmp_path,
        ExportWorkerInput(
            assignments=[
                {
                    "employee_id": "portage-mla-01",
                    "shift_template_id": "shift-morning",
                    "assignment_date": date(2026, 6, 1),
                }
            ],
            period_start=date(2026, 6, 1),
            period_end=date(2026, 6, 14),
            employees=_employees()[:1],
            shift_templates=_template_dict(),
            staff_fairness_report=report.to_dict(),
            staff_fairness_html=html_doc,
            render_breakroom_html=False,
        ),
        report_date=date(2026, 5, 30),
    )
    payload = json.loads(result.export_path.read_text(encoding="utf-8"))
    assert payload["staff_fairness_report"]["report_id"] == report.report_id
    assert payload["staff_fairness_html_path"] is not None
    assert result.staff_fairness_html_path is not None
    assert result.staff_fairness_html_path.is_file()


def test_record_staff_fairness_attestation_writes_audit_log(tmp_path: Path) -> None:
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    report = build_staff_fairness_report(
        tenant_name="Northstar",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 6, 14),
        employees=_employees()[:1],
        assignments=[
            {
                "employee_id": "portage-mla-01",
                "shift_template_id": "shift-morning",
                "assignment_date": date(2026, 6, 1) + timedelta(days=offset),
            }
            for offset in range(7)
        ],
        shift_templates=_template_dict(),
        target_hours={"portage-mla-01": 320.0},
        qual_lookup={"portage-mla-01": QUAL_MLA},
    )
    row_id = record_staff_fairness_attestation(
        conn,
        tenant_id="tenant-1",
        manager_id="manager-1",
        schedule_period_id="period-2026-summer",
        report=report,
        note="Reviewed with charge tech.",
    )
    assert row_id > 0
    row = conn.execute(
        "SELECT action_type, metadata_json FROM sys_audit_log WHERE id = ?",
        (row_id,),
    ).fetchone()
    assert row[0] == "staff_fairness_attestation"
    metadata = json.loads(row[1])
    assert metadata["schedule_period_id"] == "period-2026-summer"
    assert metadata["manager_note"] == "Reviewed with charge tech."


def test_staff_fairness_report_uses_shared_threshold_defaults() -> None:
    from lab_scheduler.validation import staff_fairness_report as report_module

    assert report_module.DEFAULT_FAIRNESS_THRESHOLDS is DEFAULT_FAIRNESS_THRESHOLDS
    assert FairnessThresholds().evening_cluster_window_days == 14
    assert FairnessThresholds().evening_cluster_max == 3
    assert FairnessThresholds().post_night_recovery_off_days == 2
