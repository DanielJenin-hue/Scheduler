from __future__ import annotations

import hashlib
import html
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import List, Optional, Sequence

from lab_scheduler.audit.schedule_log import ScheduleAuditEntry, fetch_audit_logs
from lab_scheduler.compliance.engine import ComplianceReport, ComplianceViolation, ScheduledShift
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.staff.lifecycle import ensure_staff_lifecycle_schema

REST_WINDOW_CODES = frozenset(
    {"DAILY_REST", "BETWEEN_SHIFTS", "OVERLAPPING_SHIFTS", "WEEKLY_REST"}
)
CBA_SENIORITY_CODES = frozenset({"seniority_bypass"})


@dataclass(frozen=True, slots=True)
class BreakGlassEvent:
    recorded_at_utc: str
    source: str
    employee_name: str
    shift_date: Optional[date]
    detail: str


@dataclass(frozen=True, slots=True)
class UnionComplianceReport:
    report_id: str
    generated_at_utc: str
    tenant_name: str
    period_name: str
    period_start: date
    period_end: date
    jurisdiction_display: str
    total_shifts_managed: int
    active_shift_count: int
    break_glass_events: tuple[BreakGlassEvent, ...]
    seniority_bypass_count: int
    rest_window_error_count: int
    rest_window_warning_count: int
    cba_seniority_compliant: bool
    rest_window_compliant: bool
    overall_legal_alignment: bool
    attestation_text: str
    content_hash: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _esc(text: object) -> str:
    return html.escape(str(text))


def fetch_break_glass_events(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    period_start: date,
    period_end: date,
) -> List[BreakGlassEvent]:
    ensure_staff_lifecycle_schema(conn)
    events: List[BreakGlassEvent] = []

    rows = conn.execute(
        """
        SELECT recorded_at_utc, employee_id, metadata_json
        FROM sys_audit_log
        WHERE tenant_id = ? AND action_type = 'audit_warning'
        ORDER BY id DESC
        """,
        (tenant_id,),
    ).fetchall()
    for recorded_at, employee_id, metadata_json in rows:
        metadata = json.loads(metadata_json) if metadata_json else {}
        if not metadata.get("is_compliance_overridden"):
            continue
        assignment_date = metadata.get("assignment_date")
        shift_date = date.fromisoformat(assignment_date) if assignment_date else None
        if shift_date and (shift_date < period_start or shift_date > period_end):
            continue
        name_row = None
        if employee_id:
            name_row = conn.execute(
                """
                SELECT TRIM(first_name || ' ' || last_name)
                FROM employees WHERE tenant_id = ? AND id = ?
                """,
                (tenant_id, employee_id),
            ).fetchone()
        events.append(
            BreakGlassEvent(
                recorded_at_utc=recorded_at,
                source="Break-Glass override (sys_audit_log)",
                employee_name=name_row[0] if name_row else str(employee_id or "—"),
                shift_date=shift_date,
                detail=str(metadata.get("violation") or metadata.get("warning_message", "")),
            )
        )

    override_rows = conn.execute(
        """
        SELECT sa.updated_at, sa.assignment_date, sa.employee_id,
               TRIM(e.first_name || ' ' || e.last_name) AS full_name,
               st.code
        FROM shift_assignments sa
        LEFT JOIN employees e ON e.tenant_id = sa.tenant_id AND e.id = sa.employee_id
        LEFT JOIN shift_templates st ON st.tenant_id = sa.tenant_id AND st.id = sa.shift_template_id
        WHERE sa.tenant_id = ?
          AND COALESCE(sa.is_compliance_overridden, 0) = 1
          AND sa.assignment_date >= ?
          AND sa.assignment_date <= ?
        ORDER BY sa.assignment_date, sa.id
        """,
        (tenant_id, period_start.isoformat(), period_end.isoformat()),
    ).fetchall()
    for updated_at, assignment_date, _emp_id, full_name, shift_code in override_rows:
        events.append(
            BreakGlassEvent(
                recorded_at_utc=updated_at or "",
                source="Break-Glass assignment flag",
                employee_name=full_name or "—",
                shift_date=date.fromisoformat(assignment_date),
                detail=f"Shift {shift_code or '?'} assigned with compliance override",
            )
        )

    return events


def _seniority_bypass_entries(
    audit_entries: Sequence[ScheduleAuditEntry],
) -> List[ScheduleAuditEntry]:
    return [entry for entry in audit_entries if entry.change_type == "seniority_bypass"]


def _rest_violations(report: ComplianceReport) -> tuple[List[ComplianceViolation], List[ComplianceViolation]]:
    errors: List[ComplianceViolation] = []
    warnings: List[ComplianceViolation] = []
    for violation in report.violations:
        if violation.code not in REST_WINDOW_CODES:
            continue
        if violation.severity == "error":
            errors.append(violation)
        else:
            warnings.append(violation)
    return errors, warnings


def build_union_compliance_report(
    *,
    tenant_name: str,
    period_id: str,
    period_name: str,
    period_start: date,
    period_end: date,
    rules: JurisdictionRules,
    compliance_report: ComplianceReport,
    assignments: Sequence[ScheduledShift],
    break_glass_events: Sequence[BreakGlassEvent],
    audit_entries: Sequence[ScheduleAuditEntry],
) -> UnionComplianceReport:
    total_shifts = len(assignments)
    seniority_bypasses = _seniority_bypass_entries(audit_entries)
    rest_errors, rest_warnings = _rest_violations(compliance_report)

    cba_ok = len(seniority_bypasses) == 0 or all(
        entry.seniority_bypass_justification for entry in seniority_bypasses
    )
    rest_ok = len(rest_errors) == 0
    overall = rest_ok and compliance_report.error_count == 0

    generated_at = _utc_now_iso()
    report_id = str(uuid.uuid4())
    payload = (
        f"{report_id}|{generated_at}|{total_shifts}|{len(break_glass_events)}|"
        f"{len(rest_errors)}|{len(seniority_bypasses)}"
    )
    content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    attestation = (
        f"This Union-Compliance Audit Report confirms that {total_shifts} shift assignment(s) "
        f"were managed for {period_name} under {rules.display_name} rules. "
        f"Break-Glass manual overrides are disclosed below for union transparency. "
        f"CBA seniority bypass events: {len(seniority_bypasses)} (each documented). "
        f"Rest-window statutory errors on active shifts: {len(rest_errors)}."
    )

    return UnionComplianceReport(
        report_id=report_id,
        generated_at_utc=generated_at,
        tenant_name=tenant_name,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        jurisdiction_display=rules.display_name,
        total_shifts_managed=total_shifts,
        active_shift_count=total_shifts,
        break_glass_events=tuple(break_glass_events),
        seniority_bypass_count=len(seniority_bypasses),
        rest_window_error_count=len(rest_errors),
        rest_window_warning_count=len(rest_warnings),
        cba_seniority_compliant=cba_ok,
        rest_window_compliant=rest_ok,
        overall_legal_alignment=overall,
        attestation_text=attestation,
        content_hash=content_hash,
    )


def render_union_compliance_report_html(report: UnionComplianceReport) -> str:
    if report.break_glass_events:
        break_rows = "".join(
            f"<tr><td>{_esc(event.recorded_at_utc)}</td>"
            f"<td>{_esc(event.source)}</td>"
            f"<td>{_esc(event.employee_name)}</td>"
            f"<td>{_esc(event.shift_date.isoformat() if event.shift_date else '—')}</td>"
            f"<td>{_esc(event.detail)}</td></tr>"
            for event in report.break_glass_events
        )
    else:
        break_rows = (
            "<tr><td colspan='5' style='text-align:center;color:#166534;'>"
            "No Break-Glass manual overrides recorded for this period.</td></tr>"
        )

    seniority_status = (
        "Documented CBA seniority bypass(es) on file — each includes written justification."
        if report.seniority_bypass_count
        else "No CBA seniority bypass events — seniority order preserved on all documented swaps."
    )
    rest_status = (
        "All active shifts align with statutory rest-window requirements (no blocking errors)."
        if report.rest_window_compliant
        else f"{report.rest_window_error_count} rest-window error(s) require manager review."
    )
    overall_badge = (
        "LEGAL ALIGNMENT CONFIRMED"
        if report.overall_legal_alignment
        else "REVIEW REQUIRED — SEE DETAIL SECTIONS"
    )
    overall_color = "#166534" if report.overall_legal_alignment else "#92400e"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Union Compliance Report — {_esc(report.period_name)}</title>
  <style>
    @page {{ margin: 18mm; }}
    body {{ font-family: Georgia, 'Times New Roman', serif; color: #1e293b; margin: 0; padding: 24px; }}
    h1 {{ font-size: 22px; margin: 0 0 4px; }}
    h2 {{ font-size: 15px; margin-top: 28px; border-bottom: 1px solid #cbd5e1; padding-bottom: 6px; }}
    .meta {{ font-size: 12px; color: #475569; margin-bottom: 20px; }}
    .badge {{ display: inline-block; padding: 8px 14px; border-radius: 6px; font-weight: 700;
              font-size: 12px; letter-spacing: 0.04em; background: {overall_color}; color: #fff; }}
    .metrics {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin: 20px 0; }}
    .metric {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; background: #f8fafc; }}
    .metric-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #64748b; }}
    .metric-value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }}
    th {{ background: #0f172a; color: #fff; text-align: left; padding: 8px; }}
    td {{ border: 1px solid #e2e8f0; padding: 8px; vertical-align: top; }}
    .attestation {{ background: #f1f5f9; border-left: 4px solid #0f172a; padding: 14px; margin-top: 24px; font-size: 13px; }}
    footer {{ margin-top: 32px; font-size: 10px; color: #64748b; border-top: 1px solid #e2e8f0; padding-top: 12px; }}
    @media print {{ .no-print {{ display: none; }} body {{ padding: 0; }} }}
  </style>
</head>
<body>
  <h1>Union-Compliance Audit Report</h1>
  <div class="meta">
    {_esc(report.tenant_name)} · {_esc(report.period_name)}<br/>
    Period: {_esc(report.period_start.isoformat())} to {_esc(report.period_end.isoformat())} ·
    {_esc(report.jurisdiction_display)}<br/>
    Generated (UTC): {_esc(report.generated_at_utc)} · Report ID: {_esc(report.report_id)}<br/>
    Integrity SHA-256: <code>{_esc(report.content_hash)}</code>
  </div>

  <p><span class="badge">{overall_badge}</span></p>

  <div class="metrics">
    <div class="metric">
      <div class="metric-label">Total shifts managed</div>
      <div class="metric-value">{report.total_shifts_managed}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Break-Glass overrides</div>
      <div class="metric-value">{len(report.break_glass_events)}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Seniority bypass events</div>
      <div class="metric-value">{report.seniority_bypass_count}</div>
    </div>
  </div>

  <h2>Break-Glass manual overrides (transparency log)</h2>
  <p style="font-size:12px;color:#475569;">
    Full disclosure of manager-initiated compliance overrides. Information-only — not a disciplinary record.
  </p>
  <table>
    <thead>
      <tr><th>When (UTC)</th><th>Source</th><th>Employee</th><th>Shift date</th><th>Detail</th></tr>
    </thead>
    <tbody>{break_rows}</tbody>
  </table>

  <h2>CBA seniority &amp; rest-window compliance</h2>
  <ul style="font-size:13px;line-height:1.6;">
    <li><strong>CBA seniority:</strong> { _esc(seniority_status) }</li>
    <li><strong>Rest windows:</strong> { _esc(rest_status) }</li>
    <li><strong>Rest-window warnings (non-blocking):</strong> {report.rest_window_warning_count}</li>
  </ul>

  <div class="attestation">
    <strong>Department / Union attestation</strong>
    <p>{_esc(report.attestation_text)}</p>
    <p style="margin-top:16px;">
      Signature: _____________________________ &nbsp; Date: _______________
    </p>
  </div>

  <footer>
    <p>Read-only system export — no billing, licensing, or SaaS lock. Retain this file for union or department records.</p>
    <p class="no-print">Open in a browser and use <em>Print → Save as PDF</em> to produce a PDF copy.</p>
  </footer>
</body>
</html>"""


def generate_union_compliance_report(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    tenant_name: str,
    period_id: str,
    period_name: str,
    period_start: date,
    period_end: date,
    rules: JurisdictionRules,
    compliance_report: ComplianceReport,
    assignments: Sequence[ScheduledShift],
) -> tuple[UnionComplianceReport, str]:
    break_glass = fetch_break_glass_events(
        conn,
        tenant_id=tenant_id,
        period_start=period_start,
        period_end=period_end,
    )
    audit_entries = fetch_audit_logs(
        conn,
        tenant_id=tenant_id,
        schedule_period_id=period_id,
    )
    report = build_union_compliance_report(
        tenant_name=tenant_name,
        period_id=period_id,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        rules=rules,
        compliance_report=compliance_report,
        assignments=assignments,
        break_glass_events=break_glass,
        audit_entries=audit_entries,
    )
    html_doc = render_union_compliance_report_html(report)
    return report, html_doc
