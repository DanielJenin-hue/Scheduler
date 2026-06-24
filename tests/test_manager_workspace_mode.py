from __future__ import annotations

import sqlite3
from datetime import date
from unittest.mock import patch

from lab_scheduler.compliance.audit_export import (
    ComplianceAuditSummary,
    DeflectedViolationsSummary,
    ScheduleCoverage,
    TenantMetadata,
)
from lab_scheduler.tenant.configuration import (
    MANAGER_MODE_KEY,
    ensure_tenant_configuration_schema,
    get_tenant_config_value,
    set_tenant_config_value,
)


def _sample_audit_summary(*, filled: int = 224, total: int = 336) -> ComplianceAuditSummary:
    open_slots = max(total - filled, 0)
    return ComplianceAuditSummary(
        report_id="r1",
        generated_at_utc="2026-06-01T00:00:00Z",
        tenant=TenantMetadata(
            id="acme-lab",
            name="Acme Lab",
            slug="acme-lab",
            status="active",
        ),
        period_id="period-1",
        period_name="Summer 2026",
        period_start=date(2026, 6, 1),
        period_end=date(2026, 8, 31),
        week_count=12,
        jurisdiction_display="Manitoba",
        jurisdiction_code="MB",
        statute_reference="",
        citation_label="",
        rules_evaluated=[],
        coverage=ScheduleCoverage(
            total_shift_slots=total,
            filled_slots=filled,
            open_slots=open_slots,
            assignment_count=filled,
            coverage_pct=100.0 * filled / total if total else 0.0,
            is_empty=filled == 0,
            is_partial=0 < filled < total,
        ),
        deflected=DeflectedViolationsSummary(
            total_deflected=0,
            compliance_blocked_slots=0,
            qualification_gaps=0,
        ),
        active_error_count=0,
        active_warning_count=0,
        active_violations=[],
        labor_summaries=[],
    )


def test_workspace_publish_state_from_audit_coverage() -> None:
    from scripts.app import _workspace_publish_state

    audit = _sample_audit_summary(filled=224, total=336)
    state = _workspace_publish_state(
        period_id="period-1",
        audit_summary=audit,
    )

    assert state["persist_ok"] is True
    assert state["saved_filled"] == 224
    assert state["saved_total"] == 336
    assert state["required_filled"] == 224
    assert state["required_total"] == 336
    assert state["violation_codes"] == {}


def test_is_manager_mode_defaults_on_for_regular_tenant() -> None:
    from scripts.app import _is_manager_mode

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = OFF")
    ensure_tenant_configuration_schema(conn)
    conn.commit()

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        assert _is_manager_mode(conn, "acme-lab") is True


def test_is_manager_mode_off_for_demo_ops_tenant_in_dev() -> None:
    from scripts.app import NORTHSTAR_TENANT_ID, _is_manager_mode

    conn = sqlite3.connect(":memory:")
    with patch("scripts.app.st") as mock_st, patch("scripts.app._is_production_runtime", return_value=False):
        mock_st.session_state = {}
        assert _is_manager_mode(conn, NORTHSTAR_TENANT_ID) is False


def test_is_manager_mode_on_for_demo_ops_tenant_in_production() -> None:
    from scripts.app import NORTHSTAR_TENANT_ID, _is_manager_mode

    conn = sqlite3.connect(":memory:")
    with patch("scripts.app.st") as mock_st, patch("scripts.app._is_production_runtime", return_value=True):
        mock_st.session_state = {}
        assert _is_manager_mode(conn, NORTHSTAR_TENANT_ID) is True


def test_user_facing_account_label_hides_demo_admin_on_production() -> None:
    from scripts.app import _user_facing_account_label

    with patch("scripts.app.st") as mock_st, patch("scripts.app._is_production_runtime", return_value=True):
        mock_st.session_state = {
            "username": "northstar_admin",
            "display_name": "Northstar Administrator",
        }
        assert _user_facing_account_label() == "Operator"

        mock_st.session_state = {
            "username": "jane.doe@health.mb.ca",
            "display_name": "Jane Doe",
        }
        assert _user_facing_account_label() == "Jane Doe"


def test_is_manager_mode_respects_tenant_config_and_session() -> None:
    from scripts.app import _is_manager_mode

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = OFF")
    ensure_tenant_configuration_schema(conn)
    set_tenant_config_value(
        conn,
        tenant_id="acme-lab",
        config_key=MANAGER_MODE_KEY,
        config_value="false",
    )
    conn.commit()

    with patch("scripts.app.st") as mock_st:
        mock_st.session_state = {}
        assert _is_manager_mode(conn, "acme-lab") is False

        mock_st.session_state = {"manager_mode": True}
        assert _is_manager_mode(conn, "acme-lab") is True


def test_manager_mode_config_key_round_trip() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = OFF")
    ensure_tenant_configuration_schema(conn)
    conn.commit()

    assert get_tenant_config_value(
        conn,
        tenant_id="acme-lab",
        config_key=MANAGER_MODE_KEY,
        default="true",
    ) == "true"

    set_tenant_config_value(
        conn,
        tenant_id="acme-lab",
        config_key=MANAGER_MODE_KEY,
        config_value="false",
    )
    assert get_tenant_config_value(
        conn,
        tenant_id="acme-lab",
        config_key=MANAGER_MODE_KEY,
    ) == "false"


def test_workspace_export_ready_blocked_by_audit_errors() -> None:
    from dataclasses import replace

    from scripts.app import SchedulePostingReadiness, _workspace_export_ready

    readiness = SchedulePostingReadiness(
        is_ready=True,
        attention_bullets=(),
        using_preview=False,
        last_persist_ok=True,
        has_failed_preview_available=False,
        hours_delta=0.0,
        below_evening_days=0,
        below_night_days=0,
        pending_mutations=0,
    )
    audit = replace(_sample_audit_summary(), active_error_count=3)

    assert _workspace_export_ready(posting_readiness=readiness, audit_summary=audit) is False


def test_posting_readiness_includes_audit_and_draft_compliance_errors() -> None:
    from datetime import date as date_cls

    from lab_scheduler.compliance.engine import ShiftTemplateInfo
    from scripts.app import _evaluate_schedule_posting_readiness

    period = type(
        "Period",
        (),
        {
            "period_start": date_cls(2026, 6, 1),
            "period_end_inclusive": date_cls(2026, 8, 31),
        },
    )()
    template_info = {
        "shift-d": ShiftTemplateInfo(
            id="shift-d",
            code="DAY",
            name="Day",
            start_time="07:00",
            end_time="15:00",
            duration_minutes=480,
            crosses_midnight=False,
        ),
    }
    readiness = _evaluate_schedule_posting_readiness(
        assignments=[],
        employees=[],
        period=period,
        template_info=template_info,
        hours_delta=0.0,
        compliance_error_count=2,
        audit_error_count=5,
    )

    assert readiness.is_ready is False
    assert any("saved schedule" in item for item in readiness.attention_bullets)
    assert any("this draft" in item for item in readiness.attention_bullets)
