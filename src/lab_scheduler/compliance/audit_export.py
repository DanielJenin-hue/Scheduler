from __future__ import annotations

import hashlib
import html
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import (
    ComplianceReport,
    ComplianceViolation,
    EmployeeLaborSummary,
    ScheduledShift,
    ShiftTemplateInfo,
)
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.profiles import EmployeeProfile

STATUTE_REFERENCE_BY_CODE: Dict[str, str] = {
    "MB": "Manitoba Employment Standards Code",
    "ON": "Employment Standards Act, 2000 (Ontario)",
}


@dataclass(frozen=True, slots=True)
class TenantMetadata:
    id: str
    name: str
    slug: str
    status: str


@dataclass(frozen=True, slots=True)
class ScheduleCoverage:
    total_shift_slots: int
    filled_slots: int
    open_slots: int
    assignment_count: int
    coverage_pct: float
    is_empty: bool
    is_partial: bool


@dataclass(frozen=True, slots=True)
class DeflectedViolationsSummary:
    total_deflected: int
    compliance_blocked_slots: int
    qualification_gaps: int


@dataclass(frozen=True, slots=True)
class ComplianceAuditSummary:
    report_id: str
    generated_at_utc: str
    tenant: TenantMetadata
    period_id: str
    period_name: str
    period_start: date
    period_end: date
    week_count: int
    jurisdiction_display: str
    jurisdiction_code: str
    statute_reference: str
    citation_label: str
    rules_evaluated: List[str]
    coverage: ScheduleCoverage
    deflected: DeflectedViolationsSummary
    active_error_count: int
    active_warning_count: int
    active_violations: List[ComplianceViolation]
    labor_summaries: List[EmployeeLaborSummary]
    content_hash: str = ""


def _daterange(start: date, end_inclusive: date) -> List[date]:
    days: List[date] = []
    cur = start
    while cur <= end_inclusive:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def build_rules_evaluated(rules: JurisdictionRules) -> List[str]:
    items = [
        (
            f"Weekly overtime threshold: {rules.weekly_overtime_threshold_hours:.0f} hours "
            f"({rules.overtime_rate_multiplier:.1f}× premium rate)"
        ),
        (
            f"FTE contract baseline: {rules.standard_hours_per_week_at_1_0_fte:.0f} hours/week "
            "at 1.0 FTE"
        ),
        f"Maximum consecutive work days: {rules.max_consecutive_work_days}",
        f"Maximum work days per Monday-start week: {rules.max_work_days_per_work_week}",
        f"Minimum weekly rest: {rules.min_weekly_rest_hours:.0f} hours",
    ]
    if rules.daily_overtime_threshold_hours is not None:
        items.insert(
            1,
            f"Daily overtime threshold: {rules.daily_overtime_threshold_hours:.0f} hours",
        )
    if rules.min_daily_rest_hours is not None:
        items.append(f"Minimum daily rest between shifts: {rules.min_daily_rest_hours:.0f} hours")
    if rules.min_rest_between_shifts_hours is not None:
        items.append(
            f"Minimum rest between shifts: {rules.min_rest_between_shifts_hours:.0f} hours"
        )
    if rules.max_scheduled_hours_per_day is not None:
        items.append(
            f"General daily scheduling limit: {rules.max_scheduled_hours_per_day:.0f} hours"
        )
    if rules.break_after_consecutive_hours is not None:
        items.append(
            f"Unpaid break required after {rules.break_after_consecutive_hours:.0f} consecutive hours "
            f"({rules.break_minutes} minutes)"
        )
    return items


def _compute_schedule_coverage(
    *,
    period_start: date,
    period_end: date,
    shift_template_count: int,
    assignment_count: int,
    open_slot_count: int,
) -> ScheduleCoverage:
    day_count = len(_daterange(period_start, period_end))
    total_slots = day_count * shift_template_count
    filled = max(0, total_slots - open_slot_count)
    coverage_pct = (100.0 * filled / total_slots) if total_slots else 0.0
    is_empty = assignment_count == 0
    is_partial = not is_empty and open_slot_count > 0
    return ScheduleCoverage(
        total_shift_slots=total_slots,
        filled_slots=filled,
        open_slots=open_slot_count,
        assignment_count=assignment_count,
        coverage_pct=round(coverage_pct, 1),
        is_empty=is_empty,
        is_partial=is_partial,
    )


def _count_deflected_violations(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    employees: Sequence[EmployeeProfile],
) -> DeflectedViolationsSummary:
    from lab_scheduler.scheduling.auto_generate import (
        list_open_shift_slots,
        suggest_employees_for_slot,
        validate_assignment_change,
    )

    scheduled = list(assignments)
    open_slots = list_open_shift_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=dict(shift_templates),
        assignments=scheduled,
    )

    compliance_blocked = 0
    qualification_gaps = 0

    for slot in open_slots:
        required = shift_required_qualifications.get(slot.shift_template_id, set())
        qualified = [emp for emp in employees if required.issubset(emp.qualification_ids)]
        if not qualified:
            qualification_gaps += 1
            continue

        safe = suggest_employees_for_slot(
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employees=employees,
            all_assignments=scheduled,
            shift_templates=dict(shift_templates),
            shift_required_qualifications=dict(shift_required_qualifications),
            slot_date=slot.assignment_date,
            shift_template_id=slot.shift_template_id,
            limit=1,
        )
        if safe:
            continue

        blocked = False
        for emp in qualified:
            violation = validate_assignment_change(
                rules=rules,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                employee=emp,
                all_assignments=scheduled,
                shift_templates=dict(shift_templates),
                shift_required_qualifications=dict(shift_required_qualifications),
                assignment_date=slot.assignment_date,
                new_shift_template_id=slot.shift_template_id,
            )
            if violation:
                blocked = True
                break
        if blocked:
            compliance_blocked += 1

    total = compliance_blocked + qualification_gaps
    return DeflectedViolationsSummary(
        total_deflected=total,
        compliance_blocked_slots=compliance_blocked,
        qualification_gaps=qualification_gaps,
    )


def compile_compliance_audit(
    *,
    tenant: TenantMetadata,
    period_id: str,
    period_name: str,
    period_start: date,
    period_end: date,
    week_count: int,
    rules: JurisdictionRules,
    compliance_report: ComplianceReport,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    employees: Sequence[EmployeeProfile],
    generated_at: Optional[datetime] = None,
    report_id: Optional[str] = None,
) -> ComplianceAuditSummary:
    from lab_scheduler.scheduling.auto_generate import list_open_shift_slots

    when = generated_at or datetime.now(timezone.utc).replace(microsecond=0)
    open_slots = list_open_shift_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=dict(shift_templates),
        assignments=list(assignments),
    )
    coverage = _compute_schedule_coverage(
        period_start=period_start,
        period_end=period_end,
        shift_template_count=len(shift_templates),
        assignment_count=len(assignments),
        open_slot_count=len(open_slots),
    )
    deflected = _count_deflected_violations(
        rules=rules,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=week_count,
        assignments=assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        employees=employees,
    )

    summary = ComplianceAuditSummary(
        report_id=report_id or str(uuid.uuid4()),
        generated_at_utc=when.isoformat().replace("+00:00", "Z"),
        tenant=tenant,
        period_id=period_id,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        week_count=week_count,
        jurisdiction_display=rules.display_name,
        jurisdiction_code=rules.code,
        statute_reference=STATUTE_REFERENCE_BY_CODE.get(
            rules.code, rules.citation_label
        ),
        citation_label=rules.citation_label,
        rules_evaluated=build_rules_evaluated(rules),
        coverage=coverage,
        deflected=deflected,
        active_error_count=compliance_report.error_count,
        active_warning_count=compliance_report.warning_count,
        active_violations=list(compliance_report.violations),
        labor_summaries=list(compliance_report.labor_summaries),
    )
    content_hash = _compute_content_hash(summary)
    return ComplianceAuditSummary(
        report_id=summary.report_id,
        generated_at_utc=summary.generated_at_utc,
        tenant=summary.tenant,
        period_id=summary.period_id,
        period_name=summary.period_name,
        period_start=summary.period_start,
        period_end=summary.period_end,
        week_count=summary.week_count,
        jurisdiction_display=summary.jurisdiction_display,
        jurisdiction_code=summary.jurisdiction_code,
        statute_reference=summary.statute_reference,
        citation_label=summary.citation_label,
        rules_evaluated=summary.rules_evaluated,
        coverage=summary.coverage,
        deflected=summary.deflected,
        active_error_count=summary.active_error_count,
        active_warning_count=summary.active_warning_count,
        active_violations=summary.active_violations,
        labor_summaries=summary.labor_summaries,
        content_hash=content_hash,
    )


def _summary_to_canonical_dict(summary: ComplianceAuditSummary) -> dict:
    return {
        "report_id": summary.report_id,
        "generated_at_utc": summary.generated_at_utc,
        "tenant": asdict(summary.tenant),
        "period_id": summary.period_id,
        "period_name": summary.period_name,
        "period_start": summary.period_start.isoformat(),
        "period_end": summary.period_end.isoformat(),
        "week_count": summary.week_count,
        "jurisdiction_display": summary.jurisdiction_display,
        "jurisdiction_code": summary.jurisdiction_code,
        "statute_reference": summary.statute_reference,
        "citation_label": summary.citation_label,
        "rules_evaluated": summary.rules_evaluated,
        "coverage": asdict(summary.coverage),
        "deflected": asdict(summary.deflected),
        "active_error_count": summary.active_error_count,
        "active_warning_count": summary.active_warning_count,
        "active_violations": [
            {
                "code": v.code,
                "severity": v.severity,
                "employee_id": v.employee_id,
                "employee_name": v.employee_name,
                "message": v.message,
                "rule_reference": v.rule_reference,
            }
            for v in summary.active_violations
        ],
        "labor_summaries": [
            {
                "employee_id": s.employee_id,
                "employee_name": s.employee_name,
                "fte": s.fte,
                "target_hours": round(s.target_hours, 2),
                "scheduled_hours": round(s.scheduled_hours, 2),
                "delta_hours": round(s.delta_hours, 2),
                "statutory_overtime_hours": round(s.statutory_overtime_hours, 2),
                "is_over_target_fte": s.is_over_target_fte,
                "has_statutory_violations": s.has_statutory_violations,
            }
            for s in summary.labor_summaries
        ],
    }


def _compute_content_hash(summary: ComplianceAuditSummary) -> str:
    payload = json.dumps(_summary_to_canonical_dict(summary), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _labor_status_label(summary: EmployeeLaborSummary) -> str:
    if summary.has_statutory_violations:
        return "Statutory violation"
    if summary.is_over_target_fte:
        return "Over FTE contract"
    if summary.scheduled_hours <= 0:
        return "Unscheduled"
    return "Within contract"


def render_audit_report_html(summary: ComplianceAuditSummary) -> str:
    rules_rows = "".join(f"<li>{_esc(rule)}</li>" for rule in summary.rules_evaluated)

    labor_rows: List[str] = []
    for row in summary.labor_summaries:
        status = _labor_status_label(row)
        row_class = ""
        if status in ("Statutory violation", "Over FTE contract"):
            row_class = ' class="warn-row"'
        elif status == "Unscheduled":
            row_class = ' class="muted-row"'
        labor_rows.append(
            f"<tr{row_class}>"
            f"<td>{_esc(row.employee_name)}</td>"
            f"<td class='num'>{row.fte:.1f}</td>"
            f"<td class='num'>{row.target_hours:.1f}</td>"
            f"<td class='num'>{row.scheduled_hours:.1f}</td>"
            f"<td class='num'>{row.delta_hours:+.1f}</td>"
            f"<td class='num'>{row.statutory_overtime_hours:.1f}</td>"
            f"<td>{_esc(status)}</td>"
            f"</tr>"
        )
    if not labor_rows:
        labor_rows.append(
            "<tr><td colspan='7' class='empty-note'>No active employees on roster.</td></tr>"
        )

    violation_rows: List[str] = []
    for v in summary.active_violations:
        sev = v.severity.upper()
        violation_rows.append(
            f"<tr>"
            f"<td><span class='sev sev-{_esc(v.severity)}'>{_esc(sev)}</span></td>"
            f"<td>{_esc(v.employee_name)}</td>"
            f"<td>{_esc(v.code)}</td>"
            f"<td>{_esc(v.message)}</td>"
            f"</tr>"
        )
    if not violation_rows:
        violation_rows.append(
            "<tr><td colspan='4' class='empty-note'>No active statutory violations flagged.</td></tr>"
        )

    coverage_note = "Complete coverage"
    if summary.coverage.is_empty:
        coverage_note = "No shifts scheduled — empty period"
    elif summary.coverage.is_partial:
        coverage_note = f"Partial coverage ({summary.coverage.coverage_pct:.1f}% filled)"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Union Compliance Audit — {_esc(summary.tenant.name)}</title>
  <style>
    :root {{
      --ink: #0f172a;
      --muted: #475569;
      --line: #cbd5e1;
      --panel: #f8fafc;
      --accent: #1e3a8a;
      --warn: #92400e;
      --warn-bg: #fffbeb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: "Segoe UI", Calibri, Arial, sans-serif;
      color: var(--ink);
      margin: 0;
      padding: 32px 40px 48px;
      line-height: 1.45;
      font-size: 13px;
      background: #ffffff;
    }}
    .watermark {{
      text-align: center;
      font-size: 10px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: #64748b;
      border: 1px solid var(--line);
      padding: 6px 10px;
      margin-bottom: 24px;
      background: var(--panel);
    }}
    header.report-header {{
      border-bottom: 3px solid var(--accent);
      padding-bottom: 18px;
      margin-bottom: 24px;
    }}
    h1 {{
      margin: 0 0 6px;
      font-size: 24px;
      letter-spacing: -0.02em;
    }}
    .subtitle {{
      color: var(--muted);
      font-size: 14px;
    }}
    .meta-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px 28px;
      margin: 22px 0 28px;
    }}
    .meta-card {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
      background: var(--panel);
    }}
    .meta-card h2 {{
      margin: 0 0 10px;
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
    }}
    .meta-card p {{ margin: 4px 0; }}
    section {{ margin-bottom: 28px; }}
    section h2 {{
      font-size: 15px;
      margin: 0 0 10px;
      color: var(--accent);
      border-bottom: 1px solid var(--line);
      padding-bottom: 6px;
    }}
    .metrics {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 12px;
    }}
    .metric {{
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px 14px;
      background: #fff;
    }}
    .metric .label {{
      font-size: 10px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
    }}
    .metric .value {{
      font-size: 22px;
      font-weight: 700;
      margin-top: 4px;
    }}
    ul.rules {{ margin: 8px 0 0 18px; padding: 0; }}
    ul.rules li {{ margin-bottom: 4px; }}
    table.data {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 10px;
      font-size: 12px;
    }}
    table.data th, table.data td {{
      border: 1px solid var(--line);
      padding: 8px 10px;
      vertical-align: top;
    }}
    table.data th {{
      background: #0f172a;
      color: #f8fafc;
      text-align: left;
      font-weight: 600;
    }}
    table.data td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    tr.warn-row {{ background: var(--warn-bg); }}
    tr.muted-row {{ color: var(--muted); }}
    td.empty-note {{ text-align: center; color: var(--muted); font-style: italic; }}
    .sev {{
      display: inline-block;
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.04em;
      padding: 2px 8px;
      border-radius: 999px;
    }}
    .sev-error {{ background: #fee2e2; color: #991b1b; }}
    .sev-warning {{ background: #fef3c7; color: #92400e; }}
    .attestation {{
      margin-top: 36px;
      border: 2px solid var(--ink);
      border-radius: 8px;
      padding: 20px 22px;
      page-break-inside: avoid;
    }}
    .attestation h2 {{
      margin-top: 0;
      border: none;
      padding: 0;
      color: var(--ink);
    }}
    .sig-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 28px;
      margin-top: 28px;
    }}
    .sig-line {{
      border-bottom: 1px solid var(--ink);
      min-height: 28px;
      margin-bottom: 6px;
    }}
    footer {{
      margin-top: 28px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
      font-size: 11px;
      color: var(--muted);
      page-break-inside: avoid;
    }}
    footer code {{
      font-family: Consolas, "Courier New", monospace;
      font-size: 10px;
      word-break: break-all;
    }}
    @media print {{
      body {{ padding: 18px 22px; }}
      .no-print {{ display: none; }}
    }}
  </style>
</head>
<body>
  <div class="watermark">System-generated compliance audit — unalterable record</div>

  <header class="report-header">
    <h1>Union Compliance Audit Report</h1>
    <div class="subtitle">{_esc(summary.tenant.name)} · {_esc(summary.period_name)}</div>
  </header>

  <div class="meta-grid">
    <div class="meta-card">
      <h2>Facility</h2>
      <p><strong>Tenant ID:</strong> {_esc(summary.tenant.id)}</p>
      <p><strong>Facility name:</strong> {_esc(summary.tenant.name)}</p>
      <p><strong>Slug:</strong> {_esc(summary.tenant.slug)}</p>
      <p><strong>Status:</strong> {_esc(summary.tenant.status)}</p>
    </div>
    <div class="meta-card">
      <h2>Schedule period</h2>
      <p><strong>Period ID:</strong> {_esc(summary.period_id)}</p>
      <p><strong>Window:</strong> {_esc(summary.period_start.isoformat())} to {_esc(summary.period_end.isoformat())}</p>
      <p><strong>Duration:</strong> {_esc(summary.week_count)} weeks (Monday-start)</p>
      <p><strong>Coverage status:</strong> {_esc(coverage_note)}</p>
    </div>
    <div class="meta-card">
      <h2>Jurisdiction</h2>
      <p><strong>Province:</strong> {_esc(summary.jurisdiction_display)}</p>
      <p><strong>Statute:</strong> {_esc(summary.statute_reference)}</p>
      <p><strong>Engine citation:</strong> {_esc(summary.citation_label)}</p>
    </div>
    <div class="meta-card">
      <h2>Report integrity</h2>
      <p><strong>Report ID:</strong> {_esc(summary.report_id)}</p>
      <p><strong>Generated (UTC):</strong> {_esc(summary.generated_at_utc)}</p>
      <p><strong>SHA-256:</strong> <code>{_esc(summary.content_hash)}</code></p>
    </div>
  </div>

  <section>
    <h2>Verification summary</h2>
    <div class="metrics">
      <div class="metric">
        <div class="label">Deflected violations</div>
        <div class="value">{summary.deflected.total_deflected}</div>
      </div>
      <div class="metric">
        <div class="label">Active errors</div>
        <div class="value">{summary.active_error_count}</div>
      </div>
      <div class="metric">
        <div class="label">Active warnings</div>
        <div class="value">{summary.active_warning_count}</div>
      </div>
      <div class="metric">
        <div class="label">Schedule coverage</div>
        <div class="value">{summary.coverage.coverage_pct:.1f}%</div>
      </div>
    </div>
    <p style="margin-top:12px;color:var(--muted);">
      Deflected violations include {_esc(summary.deflected.compliance_blocked_slots)} shift(s) blocked by
      labor-rule guards and {_esc(summary.deflected.qualification_gaps)} open slot(s) with no qualified
      staff available.
    </p>
  </section>

  <section>
    <h2>Rules evaluated ({_esc(summary.jurisdiction_display)})</h2>
    <ul class="rules">{rules_rows}</ul>
  </section>

  <section>
    <h2>Employee FTE contract vs scheduled hours</h2>
    <table class="data">
      <thead>
        <tr>
          <th>Employee</th>
          <th>FTE</th>
          <th>Contract target (h)</th>
          <th>Scheduled (h)</th>
          <th>Delta (h)</th>
          <th>Statutory OT (h)</th>
          <th>Status</th>
        </tr>
      </thead>
      <tbody>
        {"".join(labor_rows)}
      </tbody>
    </table>
  </section>

  <section>
    <h2>Active compliance flags</h2>
    <table class="data">
      <thead>
        <tr>
          <th>Severity</th>
          <th>Employee</th>
          <th>Code</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {"".join(violation_rows)}
      </tbody>
    </table>
  </section>

  <section class="attestation">
    <h2>Laboratory Director attestation</h2>
    <p>
      I certify that I have reviewed this system-generated Union Compliance Audit Report for
      <strong>{_esc(summary.tenant.name)}</strong>, covering schedule period
      <strong>{_esc(summary.period_name)}</strong> under
      <strong>{_esc(summary.statute_reference)}</strong>. To the best of my knowledge, the
      verification metrics, deflected-violation counts, and employee hour reconciliations accurately
      reflect the staffing data exported from the lab scheduling system at the generation timestamp
      above.
    </p>
    <div class="sig-grid">
      <div>
        <div class="sig-line"></div>
        <div>Signature</div>
      </div>
      <div>
        <div class="sig-line"></div>
        <div>Date</div>
      </div>
    </div>
    <p style="margin-top:18px;"><strong>Printed name / title:</strong> Laboratory Director</p>
  </section>

  <footer>
    <p>
      <strong>Document control:</strong> This report was generated automatically by the Lab Staffing
      Scheduler compliance engine. Any modification to this file will invalidate the SHA-256 integrity
      hash recorded above. Retain the original downloaded file for audit purposes.
    </p>
    <p class="no-print">Open this file in a browser and use <em>Print → Save as PDF</em> for archival copies.</p>
  </footer>
</body>
</html>"""


def generate_audit_export_html(summary: ComplianceAuditSummary) -> str:
    return render_audit_report_html(summary)


def generate_audit_export(
    *,
    tenant: TenantMetadata,
    period_id: str,
    period_name: str,
    period_start: date,
    period_end: date,
    week_count: int,
    rules: JurisdictionRules,
    compliance_report: ComplianceReport,
    assignments: Sequence[ScheduledShift],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    shift_required_qualifications: Mapping[str, Set[str]],
    employees: Sequence[EmployeeProfile],
) -> tuple[ComplianceAuditSummary, str]:
    summary = compile_compliance_audit(
        tenant=tenant,
        period_id=period_id,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        week_count=week_count,
        rules=rules,
        compliance_report=compliance_report,
        assignments=assignments,
        shift_templates=shift_templates,
        shift_required_qualifications=shift_required_qualifications,
        employees=employees,
    )
    html_doc = generate_audit_export_html(summary)
    return summary, html_doc
