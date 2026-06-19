from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.scheduling.schedule_tallies import is_daily_tally_row
from lab_scheduler.scheduling.contract_payroll import (
    HOURS_PER_SHIFT,
    paid_hours_per_shift,
    period_contract_hours_for_fte,
)

# "T" is the structural FTE top-up token: a short shift that closes the contract
# deficit. It is paid (counts toward FTE) but does not represent a full 12-hour tour,
# so it is deliberately excluded from WORKED_SHIFT_TOKENS coefficient counting.
FTE_TOPUP_TOKEN = "T"
PRINT_SHIFT_TOKENS: tuple[str, ...] = ("D", "E", "N", "I", "V", "T", "")
TOKEN_CELL_CODES: frozenset[str] = frozenset({"D", "E", "N", "I", "T"})
WORKED_SHIFT_TOKENS: frozenset[str] = frozenset({"D", "E", "N", "M"})
TRIAGE_ESCALATED_CELL_TAG = "[UNFILLED - ESCALATED]"
OPTIONAL_UNSTAFFED_CELL_TAG = "Unstaffed - Optional"
_CONTRACT_DISPLAY_EPSILON = 0.05

BREAKROOM_DRAFT_NOTICE = "DRAFT PREVIEW — NOT SAVED TO DATABASE"


@dataclass(frozen=True, slots=True)
class BreakroomPostingContext:
    """Export-only metadata for breakroom HTML (preview vs saved DB, trial scope)."""

    using_autopilot_preview: bool = False
    persist_ok: bool = True
    is_premium: bool = True
    required_filled: int = 0
    required_total: int = 0
    violation_codes: Mapping[str, int] = field(default_factory=dict)
    saved_filled: int = 0
    saved_total: int = 0


def is_breakroom_draft_export(
    posting_context: BreakroomPostingContext | None,
) -> bool:
    """True when the grid reflects a blocked Auto-Pilot preview (not saved DB)."""

    if posting_context is None:
        return False
    return posting_context.using_autopilot_preview and not posting_context.persist_ok


def breakroom_posting_context_from_publish_state(
    publish_state: Mapping[str, object] | None,
    *,
    is_premium: bool,
) -> BreakroomPostingContext:
    """Build posting context from workspace publish state (manager Print tab parity)."""

    if not publish_state:
        return BreakroomPostingContext(is_premium=is_premium)
    violation_raw = publish_state.get("violation_codes") or {}
    violation_codes: dict[str, int] = {}
    if isinstance(violation_raw, Mapping):
        violation_codes = {
            str(code): int(count)
            for code, count in violation_raw.items()
        }
    return BreakroomPostingContext(
        using_autopilot_preview=bool(publish_state.get("using_preview")),
        persist_ok=bool(publish_state.get("persist_ok", True)),
        is_premium=is_premium,
        required_filled=int(publish_state.get("required_filled") or 0),
        required_total=int(publish_state.get("required_total") or 0),
        violation_codes=violation_codes,
        saved_filled=int(publish_state.get("saved_filled") or 0),
        saved_total=int(publish_state.get("saved_total") or 0),
    )


def format_breakroom_meta_trial_suffix(
    *,
    week_count: int,
    is_premium: bool,
) -> str:
    """Append trial scope to header meta when the tenant is not premium."""

    if is_premium:
        return ""
    return f" ({week_count}-week trial preview)"


def resolve_breakroom_compliance_footer(
    *,
    compliance_verified_on: date | None,
    posting_context: BreakroomPostingContext | None,
    aggressive_fill_flags: Sequence[object] | None = None,
    night_streak_violations: Sequence[object] | None = None,
    work_streak_violations: Sequence[object] | None = None,
) -> tuple[str, str]:
    """Return footer badge text and CSS class for breakroom export."""

    if is_breakroom_draft_export(posting_context):
        return BREAKROOM_DRAFT_NOTICE, "breakroom-draft-badge"

    verified_on = compliance_verified_on or date.today()
    if aggressive_fill_flags:
        return (
            f"Coverage Aggressor Mode — {len(aggressive_fill_flags)} compliance flag(s) "
            f"[{verified_on.isoformat()}]",
            "breakroom-compliance-badge",
        )
    if work_streak_violations or night_streak_violations:
        total_flags = len(work_streak_violations or ()) + len(night_streak_violations or ())
        return (
            f"Streak Compliance Review — {total_flags} violation(s) "
            f"[{verified_on.isoformat()}]",
            "breakroom-compliance-badge",
        )
    return (
        f"Compliance Verified: Manitoba Labor Standards [{verified_on.isoformat()}]",
        "breakroom-compliance-badge",
    )


def format_breakroom_posting_checklist_html(
    posting_context: BreakroomPostingContext | None,
) -> str:
    """Footer checklist mirroring the manager Print tab posting requirements."""

    if posting_context is None:
        return ""

    using_preview = posting_context.using_autopilot_preview
    persist_ok = posting_context.persist_ok
    violation_codes = posting_context.violation_codes
    has_checklist_data = (
        posting_context.required_total > 0
        or posting_context.saved_total > 0
        or is_breakroom_draft_export(posting_context)
        or using_preview
    )
    if not has_checklist_data and posting_context.is_premium:
        return ""
    if using_preview and not persist_ok:
        blockers = (
            ", ".join(
                f"{code}×{count}" for code, count in sorted(violation_codes.items())
            )
            if violation_codes
            else "Yes — quality checks failed"
        )
    elif using_preview:
        blockers = "None (preview passed persist checks)"
    else:
        blockers = "None (saved database schedule)"

    required_label = (
        f"{posting_context.required_filled}/{posting_context.required_total}"
        if posting_context.required_total
        else "—"
    )
    saved_label = (
        f"{posting_context.saved_filled}/{posting_context.saved_total} slots"
        if posting_context.saved_total
        else "—"
    )
    posting_ready = (
        not is_breakroom_draft_export(posting_context)
        and posting_context.is_premium
    )
    export_label = "Ready for breakroom posting" if posting_ready else "Not ready — resolve blockers and save first"

    draft_note = ""
    if is_breakroom_draft_export(posting_context):
        draft_note = (
            "<p class='breakroom-posting-checklist-warn'>"
            "This file shows a <strong>blocked Auto-Pilot preview</strong>, not the saved "
            "database schedule. Do not post until persist blockers are cleared and the "
            "schedule is saved.</p>"
        )

    return (
        "<div class='breakroom-posting-checklist'>"
        "<div class='breakroom-posting-checklist-title'>Posting checklist</div>"
        "<ul>"
        f"<li><strong>Required clinical seats (preview):</strong> {html.escape(required_label, quote=True)}</li>"
        f"<li><strong>Persist blockers:</strong> {html.escape(blockers, quote=True)}</li>"
        f"<li><strong>Saved database schedule:</strong> {html.escape(saved_label, quote=True)}</li>"
        f"<li><strong>Breakroom export:</strong> {html.escape(export_label, quote=True)}</li>"
        "</ul>"
        f"{draft_note}"
        "</div>"
    )


def format_breakroom_draft_header_html() -> str:
    return (
        f"<div class='breakroom-draft-header'>"
        f"{html.escape(BREAKROOM_DRAFT_NOTICE, quote=True)}</div>"
    )


def format_breakroom_draft_watermark_html() -> str:
    return (
        f"<div class='breakroom-draft-watermark' aria-hidden='true'>"
        f"{html.escape(BREAKROOM_DRAFT_NOTICE, quote=True)}"
        f"</div>"
    )


def contract_hours_display_status(
    variance_hours: float,
    *,
    hours_per_shift: float,
) -> tuple[str, str]:
    """Map hour variance to footer class and short status suffix."""

    one_shift = float(hours_per_shift)
    if abs(variance_hours) < _CONTRACT_DISPLAY_EPSILON:
        return "contract-ok", "OK"
    if variance_hours < 0:
        deficit = abs(variance_hours)
        return (
            "contract-union-risk",
            f"{deficit:g}h Union Risk",
        )
    surplus = variance_hours
    if surplus <= one_shift + _CONTRACT_DISPLAY_EPSILON:
        return (
            "contract-overtime-warn",
            f"+{surplus:g}h Overtime Watch",
        )
    return (
        "contract-overtime-risk",
        f"+{surplus:g}h Overtime Risk",
    )

def _is_open_shift_cell(
    *,
    employee_id: str,
    day: date,
    open_shift_cells: Optional[Set[Tuple[str, date]]],
) -> bool:
    """Decide whether an empty cell should be flagged as an OPEN pickup.

    A blank employee cell is, by default, a legitimate day OFF (weekend / rotation
    off-day for STANDARD lines, or the off-week of a 7-on/7-off line) - NOT an
    unmet coverage seat. So we only flag a cell as OPEN when the caller has
    explicitly pinned it via ``open_shift_cells``. True coverage gaps belong to no
    employee row and are surfaced in the dedicated "Coverage Gaps" summary row
    (sourced from the archetype-aware ``list_open_shift_slots``). This avoids the
    sea of false "+OPEN" markers that shading every blank would produce.
    """
    if open_shift_cells is None:
        return False
    return (employee_id, day) in open_shift_cells


def format_open_shift_cell() -> str:
    """Render an unfilled coverage gap as a high-contrast pickup opportunity."""
    return (
        "<span class='open-shift'>"
        "<span class='open-shift-plus'>+</span>"
        "<span class='open-shift-text'>OPEN</span>"
        "</span>"
    )


# Supported breakroom paper sizes -> CSS `@page size` declarations. A true gap is
# only meaningful as a per-day "open seat" count (it belongs to no employee row),
# so coverage gaps are surfaced via a dedicated summary row rather than by shading
# individual blank employee cells (which would mislabel legitimate days off).
_PAPER_SIZE_RULES: dict[str, str] = {
    "legal": "legal landscape",       # 8.5in x 14in
    "ledger": "11in 17in landscape",  # 11in x 17in (tabloid extra)
    "letter": "letter landscape",     # 8.5in x 11in
}


def _resolve_page_size_rule(paper_size: object) -> str:
    key = str(paper_size or "legal").strip().lower()
    return _PAPER_SIZE_RULES.get(key, _PAPER_SIZE_RULES["legal"])


def build_coverage_gaps_by_day(open_slots: Iterable[Any]) -> dict[date, int]:
    """Collapse archetype-aware ``list_open_shift_slots`` output into per-day counts.

    Accepts either ``UnfilledSlot``-like objects (with an ``assignment_date``
    attribute) or mappings carrying an ``assignment_date`` key, so callers can
    feed it directly without importing the scheduling engine here.
    """
    counts: dict[date, int] = {}
    for slot in open_slots:
        day = getattr(slot, "assignment_date", None)
        if day is None and isinstance(slot, Mapping):
            day = slot.get("assignment_date")
        if isinstance(day, date):
            counts[day] = counts.get(day, 0) + 1
    return counts


def build_required_coverage_gaps_by_day(
    expanded_slots: Iterable[Any],
    fill_counts: Mapping[Any, int],
    shift_templates: Mapping[str, Any],
) -> dict[date, int]:
    """Per-day count of unfilled required demand seats (clinical floor + gate slots)."""

    from lab_scheduler.scheduling.auto_generate import _slot_required_for_coverage_gate
    from lab_scheduler.scheduling.clinical_seats import slot_is_filled

    counts: dict[date, int] = {}
    for slot in expanded_slots:
        if not _slot_required_for_coverage_gate(slot, shift_templates):
            continue
        if slot_is_filled(slot, fill_counts):
            continue
        day = getattr(slot, "assignment_date", None)
        if isinstance(day, date):
            counts[day] = counts.get(day, 0) + 1
    return counts


def format_coverage_gap_cell(count: int) -> str:
    """Render a per-day open-seat count inside the dedicated Coverage Gaps row."""
    if count <= 0:
        return "&nbsp;"
    return (
        "<span class='open-shift'>"
        f"<span class='open-shift-plus'>{count}</span>"
        "<span class='open-shift-text'>OPEN</span>"
        "</span>"
    )

PRINT_SHIFT_TOKEN_STYLES: dict[str, tuple[str, str, str]] = {
    "D": ("print-token-d", "#dbeafe", "#1e3a8a"),
    "E": ("print-token-e", "#fef3c7", "#78350f"),
    "N": ("print-token-n", "#1e293b", "#f8fafc"),
    "T": ("print-token-t", "#dcfce7", "#166534"),
}


@dataclass(frozen=True, slots=True)
class ContractTrackingRow:
    contract_line_type: str
    target_hours: float
    actual_hours: float
    variance_hours: float
    status_label: str
    status_class: str


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def template_short_to_breakroom_token(short: object) -> str:
    text = str(short or "").strip().upper()
    if text in ("", "—", "-", "OFF", "NONE", "."):
        return ""
    if text in ("M", "D"):
        return "D"
    if text in ("E", "N", "I", "V", "T"):
        return text
    return text[:1] if text[:1] in PRINT_SHIFT_TOKENS else ""


def normalize_breakroom_cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if TRIAGE_ESCALATED_CELL_TAG in text:
        return text
    text = text.upper()
    if text in ("", "—", "-", "OFF", "NONE", "."):
        return ""
    if text in ("M", "D"):
        return "D"
    if text in ("E", "N", "I", "V", "T"):
        return text
    if text in ("S", "SPECIMEN"):
        return "D"
    return text[:1] if text[:1] in PRINT_SHIFT_TOKENS else ""


def _row_cell_raw(row: Mapping[str, object], day: date) -> object:
    if day.isoformat() in row:
        return row[day.isoformat()]
    if day in row:
        return row[day]
    return ""


def count_worked_shift_tokens(
    row: Mapping[str, object],
    dates: Sequence[date],
) -> int:
    return sum(
        1
        for day in dates
        if normalize_breakroom_cell(_row_cell_raw(row, day)) in WORKED_SHIFT_TOKENS
    )


def count_topup_tokens(
    row: Mapping[str, object],
    dates: Sequence[date],
) -> int:
    return sum(
        1
        for day in dates
        if normalize_breakroom_cell(_row_cell_raw(row, day)) == FTE_TOPUP_TOKEN
    )


def compute_contract_tracking_row(
    *,
    fte: float,
    week_count: int,
    row: Mapping[str, object],
    dates: Sequence[date],
    contract_line_type: str = "",
    schedule_archetype: str = "STANDARD",
    contract_target_hours: float | None = None,
) -> ContractTrackingRow:
    if contract_target_hours is not None:
        target_hours = round(float(contract_target_hours), 1)
    else:
        target_hours = round(
            period_contract_hours_for_fte(
                fte=fte,
                weeks_in_period=week_count,
            ),
            1,
        )
    hours_per_shift = paid_hours_per_shift(schedule_archetype=schedule_archetype)
    actual_hours = round(count_worked_shift_tokens(row, dates) * hours_per_shift, 1)
    if count_topup_tokens(row, dates) > 0 and actual_hours < target_hours:
        # A top-up token closes the structural FTE deficit to exactly the contract target.
        actual_hours = target_hours
    variance_hours = round(actual_hours - target_hours, 1)
    status_class, risk_suffix = contract_hours_display_status(
        variance_hours,
        hours_per_shift=hours_per_shift,
    )
    if status_class == "contract-ok":
        status_label = f"{actual_hours:g}h / {target_hours:g}h - OK"
    elif variance_hours < 0:
        status_label = f"{actual_hours:g}h / {target_hours:g}h - {risk_suffix}"
    else:
        status_label = f"{actual_hours:g}h / {target_hours:g}h - {risk_suffix}"
    return ContractTrackingRow(
        contract_line_type=contract_line_type or "—",
        target_hours=target_hours,
        actual_hours=actual_hours,
        variance_hours=variance_hours,
        status_label=status_label,
        status_class=status_class,
    )


def infer_role_code_from_employee(employee: Mapping[str, object]) -> str:
    full_name = str(employee.get("full_name", employee.get("Employee", ""))).upper()
    if "MLT" in full_name or "mlt" in str(employee.get("id", "")).lower():
        return "MLT"
    if "MLA" in full_name or "mla" in str(employee.get("id", "")).lower():
        return "MLA"
    if "MLT" in str(employee.get("qualifications", "")).upper():
        return "MLT"
    return "MLA"


def format_schedule_employee_label(
    employee_name: str,
    *,
    role_code: str = "",
    target_hours: float | None = None,
) -> str:
    """Build the sticky Employee column label with role and period target hours inline."""

    base = str(employee_name or "").strip()
    if target_hours is None:
        return base
    hours_suffix = f"({target_hours:g}h)"
    role = str(role_code or "").strip().upper()
    if role and role not in base.upper():
        return f"{base} · {role} {hours_suffix}"
    return f"{base} {hours_suffix}"


def format_breakroom_shift_cell(token: str) -> str:
    if token == OPTIONAL_UNSTAFFED_CELL_TAG:
        return "&nbsp;"
    if " | " in token and TRIAGE_ESCALATED_CELL_TAG in token:
        # Mutually exclusive states: a cell is either an assigned shift OR an
        # unfilled-escalated gap, never both. An assignment always wins, so the
        # triage tag is rendered ONLY when there is no shift token present.
        shift_part, _, _tag_part = token.partition(" | ")
        shift_token = normalize_breakroom_cell(shift_part)
        if shift_token:
            return format_breakroom_shift_cell(shift_token)
        return (
            f"<span class='triage-escalated-tag'>{_esc(TRIAGE_ESCALATED_CELL_TAG)}</span>"
        )
    if token == TRIAGE_ESCALATED_CELL_TAG:
        return (
            f"<span class='triage-escalated-tag'>{_esc(token)}</span>"
        )
    if token in TOKEN_CELL_CODES:
        style = PRINT_SHIFT_TOKEN_STYLES.get(token)
        if style is None:
            return f"<span class='print-token'>{_esc(token)}</span>"
        class_name, background, foreground = style
        return (
            f"<span class='print-token {class_name}' "
            f"style='background:{background};color:{foreground};'>{_esc(token)}</span>"
        )
    if token:
        return _esc(token)
    return "&nbsp;"


def format_contract_line_badge(contract_line_type: str) -> str:
    line = str(contract_line_type or "—").strip().upper() or "—"
    return (
        f"<span class='union-line-badge union-line-{line.replace('/', '-').lower()}'>"
        f"{_esc(line)}</span>"
    )


def format_contract_tracking_cell(tracking: ContractTrackingRow) -> str:
    target_text = _esc(f"{tracking.target_hours:g}h target")
    actual_text = _esc(f"{tracking.actual_hours:g}h actual")
    return (
        f"<div class='contract-tracking-cell'>"
        f"{format_contract_line_badge(tracking.contract_line_type)}"
        f"<span class='contract-hours'>{target_text}</span>"
        f"<span class='contract-hours'>{actual_text}</span>"
        f"<span class='contract-status {tracking.status_class}'>{_esc(tracking.status_label)}</span>"
        f"</div>"
    )


def generate_breakroom_print_html(
    *,
    facility_name: str,
    period_name: str,
    period_start: date,
    period_end: date,
    week_count: int,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    schedule_rows: Sequence[Mapping[str, object]],
    compliance_verified_on: date | None = None,
    aggressive_fill_flags: Sequence[object] | None = None,
    night_streak_violations: Sequence[object] | None = None,
    work_streak_violations: Sequence[object] | None = None,
    schedule_archetype: str = "STANDARD",
    open_shift_cells: Optional[Set[Tuple[str, date]]] = None,
    coverage_gaps_by_day: Optional[Mapping[date, int]] = None,
    paper_size: str = "legal",
    contract_target_hours_by_employee: Optional[Mapping[str, float]] = None,
    posting_context: BreakroomPostingContext | None = None,
) -> str:
    """Render a breakroom-ready printable schedule grid (Legal/Ledger landscape).

    ``coverage_gaps_by_day`` (recommended): per-day count of true unfilled
    coverage seats, sourced from the archetype-aware ``list_open_shift_slots``
    (see :func:`build_coverage_gaps_by_day`). When any day has a gap, a dedicated
    shaded "Coverage Gaps" summary row is appended. This is the only place a gap
    is semantically meaningful in an employee x date grid.

    ``open_shift_cells`` (optional override): pins exactly which blank
    (employee_id, date) cells should be flagged as open pickups. When omitted,
    all blank employee cells stay visually quiet (they are legitimate days off),
    and true coverage gaps are shown only via ``coverage_gaps_by_day``.

    ``paper_size``: one of ``"legal"`` (default), ``"ledger"``, or ``"letter"``;
    drives the active ``@page`` size so Ledger sizing actually applies.
    """

    from lab_scheduler.scheduling.coverage_aggressor import format_aggressive_fill_flags_html
    from lab_scheduler.scheduling.night_streak_corrector import (
        format_night_streak_violations_html,
    )
    from lab_scheduler.scheduling.streak_validator import format_work_streak_violations_html

    is_draft = is_breakroom_draft_export(posting_context)
    is_premium = True if posting_context is None else posting_context.is_premium
    meta_trial_suffix = format_breakroom_meta_trial_suffix(
        week_count=week_count,
        is_premium=is_premium,
    )
    compliance_badge, compliance_badge_class = resolve_breakroom_compliance_footer(
        compliance_verified_on=compliance_verified_on,
        posting_context=posting_context,
        aggressive_fill_flags=aggressive_fill_flags,
        night_streak_violations=night_streak_violations,
        work_streak_violations=work_streak_violations,
    )
    if aggressive_fill_flags:
        flags_html = format_aggressive_fill_flags_html(aggressive_fill_flags)
    elif work_streak_violations or night_streak_violations:
        flags_html = "".join(
            part
            for part in (
                format_work_streak_violations_html(work_streak_violations or ()),
                format_night_streak_violations_html(night_streak_violations or ()),
            )
            if part
        )
    else:
        flags_html = ""
    posting_checklist_html = format_breakroom_posting_checklist_html(posting_context)
    draft_header_html = format_breakroom_draft_header_html() if is_draft else ""
    draft_watermark_html = format_breakroom_draft_watermark_html() if is_draft else ""
    employee_meta = {str(emp.get("id", "")): emp for emp in employees}
    header_cells = "".join(
        f"<th class='day-col{' week-start' if d.weekday() == 0 else ''}'>{_esc(d.strftime('%m/%d'))}<br>{_esc(d.strftime('%a'))}</th>"
        for d in dates
    )
    body_rows: list[str] = []
    for row in schedule_rows:
        emp_id = str(row.get("employee_id", ""))
        tally_row = is_daily_tally_row(row)
        meta = employee_meta.get(emp_id, {})
        fte = float(meta.get("fte", row.get("fte", 1.0)) or 1.0)
        contract_line = str(
            meta.get("contract_line_type", row.get("contract_line_type", "")) or ""
        )
        contract_target = (
            float(contract_target_hours_by_employee[emp_id])
            if contract_target_hours_by_employee and emp_id in contract_target_hours_by_employee
            else None
        )
        tracking = compute_contract_tracking_row(
            fte=fte,
            week_count=week_count,
            row=row,
            dates=dates,
            contract_line_type=contract_line,
            schedule_archetype=schedule_archetype,
            contract_target_hours=contract_target,
        )
        if tally_row:
            employee_name = _esc(row.get("Employee", ""))
        else:
            role_code = infer_role_code_from_employee(
                {**meta, "Employee": row.get("Employee", ""), "id": emp_id}
            )
            employee_name = _esc(
                format_schedule_employee_label(
                    str(row.get("Employee", "")),
                    role_code=role_code,
                    target_hours=tracking.target_hours,
                )
            )
        cells: list[str] = []
        for d in dates:
            raw = _row_cell_raw(row, d)
            if tally_row:
                display = _esc(str(raw if raw not in ("", None) else "0"))
                week_class = " week-start" if d.weekday() == 0 else ""
                cells.append(
                    f"<td class='shift-cell tally-cell{week_class}'>{display}</td>"
                )
                continue
            token = normalize_breakroom_cell(raw)
            week_class = " week-start" if d.weekday() == 0 else ""
            if not token and _is_open_shift_cell(
                employee_id=emp_id,
                day=d,
                open_shift_cells=open_shift_cells,
            ):
                cells.append(
                    f"<td class='shift-cell shift-cell--open{week_class}'>{format_open_shift_cell()}</td>"
                )
            else:
                cells.append(
                    f"<td class='shift-cell{week_class}'>{format_breakroom_shift_cell(token)}</td>"
                )
        row_class = " class='tally-row'" if tally_row else ""
        contract_cell = (
            "<td class='contract-tracking-col'>&nbsp;</td>"
            if tally_row
            else f"<td class='contract-tracking-col'>{format_contract_tracking_cell(tracking)}</td>"
        )
        body_rows.append(
            f"<tr{row_class}>"
            f"<td class='emp-col{' tally-label' if tally_row else ''}'>{employee_name}</td>"
            f"{''.join(cells)}"
            f"{contract_cell}"
            "</tr>"
        )

    # Option A (recommended): a true coverage gap belongs to no employee row, so
    # surface it as a dedicated per-day summary row sourced from the archetype-aware
    # list_open_shift_slots. Rendered only when at least one day has an open seat.
    if coverage_gaps_by_day and any(int(v or 0) > 0 for v in coverage_gaps_by_day.values()):
        gap_cells: list[str] = []
        for d in dates:
            count = int(coverage_gaps_by_day.get(d, 0) or 0)
            week_class = " week-start" if d.weekday() == 0 else ""
            cell_class = "shift-cell coverage-gap-cell" if count > 0 else "shift-cell"
            gap_cells.append(
                f"<td class='{cell_class}{week_class}'>{format_coverage_gap_cell(count)}</td>"
            )
        total_gaps = sum(int(v or 0) for v in coverage_gaps_by_day.values())
        body_rows.append(
            "<tr class='coverage-gap-row'>"
            "<td class='emp-col tally-label'>Coverage Gaps (open seats)</td>"
            f"{''.join(gap_cells)}"
            f"<td class='contract-tracking-col'>{_esc(total_gaps)} open total</td>"
            "</tr>"
        )

    page_size_rule = _resolve_page_size_rule(paper_size)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{_esc(facility_name)} · Breakroom Schedule</title>
  <style>
    :root {{
      color-scheme: light;
    }}
    @page {{
      size: {page_size_rule};
      margin: 0.35in;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      padding: 0;
      font-family: "Arial", "Helvetica Neue", sans-serif;
      color: #000000;
      background: #ffffff;
    }}
    .breakroom-sheet {{
      width: 100%;
    }}
    .breakroom-header {{
      border-bottom: 2px solid #000000;
      margin-bottom: 10px;
      padding-bottom: 8px;
    }}
    .breakroom-header h1 {{
      margin: 0 0 4px 0;
      font-size: 22px;
      font-weight: 800;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }}
    .breakroom-header .meta {{
      font-size: 12px;
      font-weight: 700;
    }}
    .breakroom-grid-wrap {{
      overflow: hidden;
    }}
    .breakroom-screen-bar {{
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 10px;
      background: #0f172a;
      color: #e2e8f0;
      font-size: 12px;
      position: sticky;
      top: 0;
      z-index: 10000;
    }}
    .breakroom-screen-bar button {{
      border: 1px solid #64748b;
      background: #1e293b;
      color: #f8fafc;
      border-radius: 6px;
      padding: 5px 12px;
      font-weight: 700;
      cursor: pointer;
    }}
    .breakroom-screen-bar button:hover {{
      border-color: #93c5fd;
      color: #ffffff;
    }}
    body.breakroom-screen-fit {{
      overflow: hidden;
      height: 100vh;
    }}
    body.breakroom-screen-fit .breakroom-sheet {{
      height: calc(100vh - 44px);
      display: flex;
      flex-direction: column;
    }}
    body.breakroom-screen-fit .breakroom-header,
    body.breakroom-screen-fit .breakroom-footer,
    body.breakroom-screen-fit .breakroom-posting-checklist,
    body.breakroom-screen-fit .aggressive-fill-flags,
    body.breakroom-screen-fit .breakroom-compliance-badge {{
      flex: 0 0 auto;
    }}
    body.breakroom-screen-fit .breakroom-grid-wrap {{
      flex: 1 1 auto;
      min-height: 0;
      position: relative;
    }}
    body.breakroom-screen-fit table.breakroom-grid {{
      transform-origin: top left;
    }}
    table.breakroom-grid {{
      width: 100%;
      border-collapse: collapse;
      table-layout: fixed;
      font-size: 10px;
    }}
    table.breakroom-grid th,
    table.breakroom-grid td {{
      border: 1px solid #000000;
      text-align: center;
      vertical-align: middle;
      padding: 6px 4px;
    }}
    table.breakroom-grid thead th {{
      background: #ffffff;
      color: #000000;
      font-weight: 800;
      font-size: 9px;
      line-height: 1.15;
    }}
    table.breakroom-grid tbody td.emp-col {{
      text-align: left;
      font-weight: 700;
      font-size: 10px;
      padding-left: 6px;
      width: 180px;
      min-width: 180px;
      white-space: normal;
      overflow: visible;
      text-overflow: clip;
    }}
    table.breakroom-grid tbody tr:nth-child(even) {{
      background-color: #f2f2f2;
    }}
    table.breakroom-grid tbody tr.tally-row {{
      background-color: #dbeafe;
      font-weight: 800;
    }}
    table.breakroom-grid tbody tr.tally-row td.tally-label {{
      font-weight: 800;
    }}
    table.breakroom-grid tbody tr.tally-row td.tally-cell {{
      font-weight: 800;
      font-size: 11px;
    }}
    table.breakroom-grid td.shift-cell {{
      font-weight: 900;
      font-size: 13px;
      letter-spacing: 0.08em;
      height: 26px;
      padding: 6px 5px;
    }}
    table.breakroom-grid .print-token {{
      display: inline-block;
      min-width: 20px;
      padding: 3px 6px;
      border: 2px solid #000000;
      font-family: "Arial Black", "Arial", sans-serif;
      font-weight: 900;
      font-size: 13px;
      letter-spacing: 0.1em;
      line-height: 1.1;
    }}
    table.breakroom-grid td.shift-cell--open {{
      background-image: repeating-linear-gradient(
        45deg, #f3f4f6, #f3f4f6 3px, #ffffff 3px, #ffffff 7px
      );
    }}
    table.breakroom-grid tbody tr.coverage-gap-row {{
      background-color: #fff7ed;
      font-weight: 800;
    }}
    table.breakroom-grid tbody tr.coverage-gap-row td.emp-col {{
      font-weight: 800;
      color: #9a3412;
    }}
    table.breakroom-grid td.coverage-gap-cell {{
      background-image: repeating-linear-gradient(
        45deg, #fed7aa, #fed7aa 3px, #ffffff 3px, #ffffff 7px
      );
    }}
    table.breakroom-grid .open-shift {{
      display: inline-flex;
      flex-direction: column;
      align-items: center;
      line-height: 1;
    }}
    table.breakroom-grid .open-shift-plus {{
      font-family: "Arial Black", "Arial", sans-serif;
      font-size: 15px;
      font-weight: 900;
      color: #111827;
    }}
    table.breakroom-grid .open-shift-text {{
      font-size: 6.5px;
      font-weight: 800;
      letter-spacing: 0.12em;
      color: #374151;
    }}
    table.breakroom-grid .print-token-d {{
      background: #dbeafe;
      color: #1e3a8a;
    }}
    table.breakroom-grid .print-token-e {{
      background: #fef3c7;
      color: #78350f;
    }}
    table.breakroom-grid .print-token-n {{
      background: #1e293b;
      color: #f8fafc;
    }}
    table.breakroom-grid .print-token-t {{
      background: #dcfce7;
      color: #166534;
    }}
    table.breakroom-grid .triage-escalated-tag {{
      display: inline-block;
      padding: 1px 3px;
      border: 2px solid #991b1b;
      background: #fee2e2;
      color: #991b1b;
      font-size: 7px;
      font-weight: 900;
      letter-spacing: 0.02em;
      line-height: 1.15;
      white-space: normal;
    }}
    table.breakroom-grid .week-start {{
      border-left-width: 2px;
    }}
    table.breakroom-grid th.contract-tracking-col,
    table.breakroom-grid td.contract-tracking-col {{
      width: 148px;
      min-width: 148px;
      text-align: left;
      padding: 4px 6px;
      border-left: 2px solid #000000;
      vertical-align: middle;
    }}
    table.breakroom-grid thead th.contract-tracking-col {{
      font-size: 8px;
      line-height: 1.2;
      text-transform: uppercase;
    }}
    .contract-tracking-cell {{
      display: flex;
      flex-direction: column;
      gap: 3px;
      align-items: flex-start;
    }}
    .union-line-badge {{
      display: inline-block;
      padding: 1px 5px;
      border: 1.5px solid #000000;
      font-size: 8px;
      font-weight: 900;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      background: #ffffff;
    }}
    .union-line-de {{ background: #eff6ff; }}
    .union-line-dn {{ background: #f5f3ff; }}
    .union-line-m-f {{ background: #ecfdf5; }}
    .contract-hours {{
      font-size: 8px;
      font-weight: 700;
      color: #1f2937;
    }}
    .contract-status {{
      display: inline-block;
      padding: 2px 5px;
      border-radius: 3px;
      font-size: 8px;
      font-weight: 800;
      line-height: 1.2;
      border: 1px solid transparent;
    }}
    .contract-ok {{
      background: #dcfce7;
      color: #166534;
      border-color: #86efac;
    }}
    .contract-union-risk {{
      background: #fee2e2;
      color: #991b1b;
      border-color: #fca5a5;
    }}
    .contract-overtime-warn {{
      background: #ffedd5;
      color: #9a3412;
      border-color: #fdba74;
    }}
    .contract-overtime-risk {{
      background: #fee2e2;
      color: #991b1b;
      border-color: #fca5a5;
    }}
    .breakroom-footer {{
      margin-top: 10px;
      border-top: 2px solid #000000;
      padding-top: 8px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }}
    .breakroom-compliance-badge {{
      margin-top: 8px;
      padding-top: 8px;
      border-top: 1px solid #000000;
      font-size: 11px;
      font-weight: 800;
    }}
    .breakroom-draft-badge {{
      margin-top: 8px;
      padding: 10px 12px;
      border: 3px solid #000000;
      background: #7f1d1d;
      color: #ffffff;
      font-size: 13px;
      font-weight: 900;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .breakroom-draft-header {{
      margin: 0 0 10px 0;
      padding: 10px 12px;
      border: 3px solid #000000;
      background: #991b1b;
      color: #ffffff;
      font-size: 14px;
      font-weight: 900;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      text-align: center;
    }}
    .breakroom-draft-watermark {{
      position: fixed;
      top: 38%;
      left: 6%;
      transform: rotate(-24deg);
      font-size: 34px;
      color: rgba(153, 27, 27, 0.22);
      font-weight: 900;
      z-index: 9999;
      pointer-events: none;
      letter-spacing: 0.05em;
      max-width: 90%;
      line-height: 1.2;
      text-align: center;
    }}
    .breakroom-posting-checklist {{
      margin-top: 10px;
      padding: 10px 12px;
      border: 2px solid #000000;
      background: #f9fafb;
      font-size: 10px;
      font-weight: 700;
      line-height: 1.45;
    }}
    .breakroom-posting-checklist-title {{
      font-size: 11px;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 6px;
    }}
    .breakroom-posting-checklist ul {{
      margin: 0;
      padding-left: 18px;
    }}
    .breakroom-posting-checklist-warn {{
      margin: 8px 0 0 0;
      padding: 8px;
      border: 2px solid #b45309;
      background: #fffbeb;
      color: #92400e;
      font-weight: 800;
    }}
    .aggressive-fill-flags {{
      margin: 0 0 16px 0;
      padding: 12px 14px;
      border: 2px solid #b45309;
      background: #fffbeb;
      border-radius: 6px;
    }}
    .aggressive-fill-flags h2 {{
      margin: 0 0 8px 0;
      font-size: 14px;
      color: #92400e;
    }}
    .aggressive-fill-note {{
      margin: 0 0 8px 0;
      font-size: 11px;
      color: #78350f;
    }}
    .aggressive-fill-list {{
      margin: 0;
      padding-left: 18px;
      font-size: 10px;
      max-height: 180px;
      overflow: auto;
    }}
    .no-screen-controls {{
      display: none;
    }}
    @media screen {{
      body {{
        padding: 18px;
      }}
      .print-hint {{
        margin-bottom: 12px;
        font-size: 12px;
        color: #334155;
      }}
    }}
    @media print {{
      @page {{
        size: {page_size_rule};
        margin: 0.35in;
      }}
      * {{
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
      }}
      .print-hint,
      .no-print {{
        display: none !important;
      }}
      body {{
        padding: 0;
      }}
      /* Mute interior grid lines so ink does not bleed on cheap printers. */
      table.breakroom-grid th,
      table.breakroom-grid td {{
        border-color: #E0E0E0 !important;
      }}
      /* Keep a full staff member's row intact across page breaks. */
      thead {{
        display: table-header-group;
      }}
      table.breakroom-grid tr {{
        page-break-inside: avoid !important;
        break-inside: avoid !important;
      }}
      table.breakroom-grid .print-token-d {{
        background: #dbeafe !important;
        color: #1e3a8a !important;
      }}
      table.breakroom-grid .print-token-e {{
        background: #fef3c7 !important;
        color: #78350f !important;
      }}
      table.breakroom-grid .print-token-n {{
        background: #1e293b !important;
        color: #f8fafc !important;
      }}
      table.breakroom-grid .print-token-t {{
        background: #dcfce7 !important;
        color: #166534 !important;
      }}
    }}
  </style>
</head>
<body>
  {draft_watermark_html}
  <div class="breakroom-screen-bar no-print">
    <button type="button" id="breakroom-enter-fs">⛶ Fullscreen</button>
    <button type="button" id="breakroom-exit-fs" hidden>✕ Exit fullscreen</button>
    <button type="button" id="breakroom-fit-screen">Fit to screen</button>
    <span id="breakroom-screen-hint">Esc exits browser fullscreen</span>
  </div>
  <div class="breakroom-sheet">
    <div class="print-hint no-print">
      Open browser print dialog and choose Legal or Ledger landscape for best breakroom output.
    </div>
    {draft_header_html}
    <header class="breakroom-header">
      <h1>{_esc(facility_name)}</h1>
      <div class="meta">
        {_esc(period_name)} · {_esc(period_start.isoformat())} to {_esc(period_end.isoformat())}
        · {_esc(week_count)}-week Monday-start block{_esc(meta_trial_suffix)}
      </div>
    </header>
    {flags_html}
    <div class="breakroom-grid-wrap">
      <table class="breakroom-grid">
        <thead>
          <tr>
            <th class="emp-col">Employee</th>
            {header_cells}
            <th class="contract-tracking-col">Contract Tracking<br>Line · Target · Actual · Status</th>
          </tr>
        </thead>
        <tbody>
          {''.join(body_rows)}
        </tbody>
      </table>
    </div>
    <footer class="breakroom-footer">
      Shift legend: D = Day · E = Evening · N = Night · T = FTE Top-up · I = Off (Sick) · V = Off (Vacation) · + OPEN = Open shift (pickup available)
      · Contract lines: D/E = Day+Evening · D/N = Day+Night · M-F = Monday–Friday Day only
    </footer>
    {posting_checklist_html}
    <div class="{compliance_badge_class}">{_esc(compliance_badge)}</div>
  </div>
  <script>
  (function () {{
    function fitBreakroomToScreen() {{
      document.body.classList.add("breakroom-screen-fit");
      var wrap = document.querySelector(".breakroom-grid-wrap");
      var table = document.querySelector("table.breakroom-grid");
      if (!wrap || !table) return;
      table.style.transform = "none";
      var availW = wrap.clientWidth;
      var availH = wrap.clientHeight;
      var natW = table.offsetWidth;
      var natH = table.offsetHeight;
      if (availW <= 0 || availH <= 0 || natW <= 0 || natH <= 0) return;
      var scale = Math.min(availW / natW, availH / natH);
      table.style.transformOrigin = "top left";
      table.style.transform = "scale(" + scale + ")";
    }}
    function requestBreakroomFullscreen() {{
      var root = document.documentElement;
      var fn = root.requestFullscreen || root.webkitRequestFullscreen;
      if (fn) Promise.resolve(fn.call(root)).catch(function () {{}});
    }}
    function exitBreakroomFullscreen() {{
      var exitFn = document.exitFullscreen || document.webkitExitFullscreen;
      if (exitFn) Promise.resolve(exitFn.call(document)).catch(function () {{}});
    }}
    function updateBreakroomFsButtons() {{
      var active = !!(document.fullscreenElement || document.webkitFullscreenElement);
      var fsBtn = document.getElementById("breakroom-enter-fs");
      var exitBtn = document.getElementById("breakroom-exit-fs");
      var hint = document.getElementById("breakroom-screen-hint");
      if (fsBtn) fsBtn.hidden = active;
      if (exitBtn) exitBtn.hidden = !active;
      if (hint) {{
        hint.textContent = active
          ? "Press Esc or click ✕ Exit fullscreen"
          : "Esc exits browser fullscreen";
      }}
    }}
    var fsBtn = document.getElementById("breakroom-enter-fs");
    var exitFsBtn = document.getElementById("breakroom-exit-fs");
    var fitBtn = document.getElementById("breakroom-fit-screen");
    if (fsBtn) fsBtn.addEventListener("click", requestBreakroomFullscreen);
    if (exitFsBtn) exitFsBtn.addEventListener("click", exitBreakroomFullscreen);
    if (fitBtn) fitBtn.addEventListener("click", fitBreakroomToScreen);
    document.addEventListener("fullscreenchange", function () {{
      updateBreakroomFsButtons();
      if (document.body.classList.contains("breakroom-screen-fit")) fitBreakroomToScreen();
    }});
    window.addEventListener("resize", function () {{
      if (document.body.classList.contains("breakroom-screen-fit")) fitBreakroomToScreen();
    }});
    updateBreakroomFsButtons();
  }})();
  </script>
</body>
</html>"""
