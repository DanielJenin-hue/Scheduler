"""Compact shift-placement summary for Auto-Pilot runs (UI + diagnostics)."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Mapping, Optional, Sequence

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.demand import (
    expand_schedule_slots,
    infer_qual_code,
    is_clinical_floor_pool,
    portage_concurrent_demands,
)
from lab_scheduler.scheduling.auto_generate import (
    PlannedAssignment,
    _seat_fill_counts,
    _slot_required_for_coverage_gate,
)
from lab_scheduler.scheduling.clinical_seats import slot_is_filled
from lab_scheduler.scheduling.contract_payroll import apply_catalog_targets_for_vacant_master_lines
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import (
    find_portage_operational_tally_violations,
    shift_band_from_template_code,
)


def _period_date_keys(period_start: date, period_end: date) -> list[str]:
    keys: list[str] = []
    cursor = period_start
    while cursor <= period_end:
        keys.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return keys


def _daily_band_counts(
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    period_start: date,
    period_end: date,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    days = {key: 0 for key in _period_date_keys(period_start, period_end)}
    evenings = dict(days)
    nights = dict(days)
    for assignment in assignments:
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        key = assignment.assignment_date.isoformat()
        if key not in days:
            continue
        band = _band_label(template.code)
        if band == "D":
            days[key] += 1
        elif band == "E":
            evenings[key] += 1
        elif band == "N":
            nights[key] += 1
    return days, evenings, nights


def build_daily_tally_rows(
    assignments: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    *,
    period_start: date,
    period_end: date,
) -> list[dict[str, object]]:
    """Mirror the grid footer: Total Days / Evenings / Nights per calendar date."""

    days, evenings, nights = _daily_band_counts(
        assignments,
        shift_templates,
        period_start=period_start,
        period_end=period_end,
    )
    date_keys = _period_date_keys(period_start, period_end)
    return [
        {"Row": "Total Days", **{key: days[key] for key in date_keys}},
        {"Row": "Total Evenings", **{key: evenings[key] for key in date_keys}},
        {"Row": "Total Nights", **{key: nights[key] for key in date_keys}},
    ]


def _band_label(template_code: str) -> str:
    return shift_band_from_template_code(template_code) or template_code[:1]


def _day_kind(assignment_date: date) -> str:
    return "weekend" if assignment_date.weekday() >= 5 else "weekday"


def _unfilled_slot_breakdown(
    expanded_slots: Sequence[object],
    fill_counts: Mapping[tuple[date, str, Optional[str]], int],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> Counter[str]:
    counts: Counter[str] = Counter()
    for slot in expanded_slots:
        if not _slot_required_for_coverage_gate(slot, shift_templates):
            continue
        if slot_is_filled(slot, fill_counts):
            continue
        template = shift_templates[slot.shift_id]
        pool = "clinical_floor" if is_clinical_floor_pool(slot.role_pool_id) else "other"
        weekend = _day_kind(slot.assignment_date)
        qual = slot.required_qual_code or "ANY"
        counts[f"{weekend}:{template.code}:{pool}:{qual}"] += 1
    return counts


@dataclass(slots=True)
class AutoPilotShiftSummary:
    total_shifts: int = 0
    required_slots_filled: int = 0
    required_slots_total: int = 0
    open_gap_count: int = 0
    slots_filled: int = 0
    slots_total: int = 0
    persist_ok: bool = False
    cpsat_status: str = ""
    post_cpsat_healing_skipped: bool = False
    by_band: dict[str, int] = field(default_factory=dict)
    by_band_weekday: dict[str, dict[str, int]] = field(default_factory=dict)
    by_role_band: dict[str, int] = field(default_factory=dict)
    per_line: list[dict[str, object]] = field(default_factory=list)
    daily_tally_rows: list[dict[str, object]] = field(default_factory=list)
    en_tally_violation_count: int = 0
    en_tally_underfill_count: int = 0
    en_tally_overfill_count: int = 0
    en_tally_samples: list[str] = field(default_factory=list)
    en_tally_underfill_samples: list[str] = field(default_factory=list)
    en_tally_overfill_samples: list[str] = field(default_factory=list)
    clinical_gap_count: int = 0
    top_unfilled: list[dict[str, object]] = field(default_factory=list)
    violation_codes: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def compute_auto_pilot_shift_summary(
    *,
    assignments: Sequence[PlannedAssignment],
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    qual_codes: Mapping[str, str],
    template_id_to_band: Mapping[str, str],
    required_slots_filled: int = 0,
    required_slots_total: int = 0,
    slots_filled: int = 0,
    slots_total: int = 0,
    open_gap_count: int = 0,
    cpsat_status: str = "",
    post_cpsat_healing_skipped: bool = False,
    clinical_gap_count: int = 0,
    persist_ok: bool = False,
    violation_codes: Optional[Mapping[str, int]] = None,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    rules=None,
) -> AutoPilotShiftSummary:
    """Summarize where Auto-Pilot placed shifts for manager review."""

    by_band: Counter[str] = Counter()
    by_band_weekday: dict[str, Counter[str]] = {
        "weekday": Counter(),
        "weekend": Counter(),
    }
    by_role_band: Counter[str] = Counter()
    employees_by_id = {employee.id: employee for employee in employees}

    for assignment in assignments:
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        band = _band_label(template.code)
        day_kind = _day_kind(assignment.assignment_date)
        by_band[band] += 1
        by_band_weekday[day_kind][band] += 1
        employee = employees_by_id.get(assignment.employee_id)
        role = infer_qual_code(employee, qual_codes=qual_codes) if employee else "?"
        by_role_band[f"{role}-{band}"] += 1

    catalog_targets: Mapping[str, float] = {}
    weekend_targets: Mapping[str, int] = {}
    if employee_target_hours and rules is not None:
        catalog_targets = apply_catalog_targets_for_vacant_master_lines(
            employees,
            employee_target_hours,
            rules=rules,
            weeks_in_period=weeks_in_period,
            period_start=period_start,
            period_end=period_end,
        )
        from lab_scheduler.scheduling.portage_equity_targets import (
            build_vacant_line_weekend_target_map,
        )

        weekend_targets = build_vacant_line_weekend_target_map(
            employees,
            catalog_targets,
            qual_codes,
            period_start=period_start,
            period_end=period_end,
        )

    per_line: list[dict[str, object]] = []
    line_band_counts: dict[str, Counter[str]] = {}
    line_hours: dict[str, float] = {}
    for assignment in assignments:
        employee = employees_by_id.get(assignment.employee_id)
        if employee is None:
            continue
        template = shift_templates.get(assignment.shift_template_id)
        if template is None:
            continue
        line_band_counts.setdefault(employee.id, Counter())
        line_band_counts[employee.id][_band_label(template.code)] += 1
        line_hours[employee.id] = line_hours.get(employee.id, 0.0) + (
            template.duration_minutes / 60.0
        )

    def _line_sort_key(employee: EmployeeProfile) -> tuple:
        vacant = parse_vacant_portage_line(employee.full_name)
        if vacant is not None:
            role, contract, line_num = vacant
            return (0, role, contract, line_num, employee.full_name)
        return (1, employee.full_name)

    for employee in sorted(employees, key=_line_sort_key):
        bands = line_band_counts.get(employee.id, Counter())
        if not bands and employee.id not in line_hours:
            continue
        target = float(catalog_targets.get(employee.id, 0.0))
        if target <= 0.0 and employee_target_hours is not None:
            target = float(employee_target_hours.get(employee.id, 0.0))
        total_shifts = sum(bands.values())
        contract = (employee.contract_line_type or "D/E").upper()
        alt_band = "E" if contract == "D/E" else "N"
        alt_shifts = int(bands.get(alt_band, 0))
        weekend_scheduled = sum(
            1
            for assignment in assignments
            if assignment.employee_id == employee.id
            and period_start <= assignment.assignment_date <= period_end
            and assignment.assignment_date.weekday() >= 5
            and _band_label(shift_templates[assignment.shift_template_id].code)
            in ("D", "E", "N")
            if assignment.shift_template_id in shift_templates
        )
        weekend_target = int(weekend_targets.get(employee.id, 0))
        per_line.append(
            {
                "line": employee.full_name,
                "D": int(bands.get("D", 0)),
                "E": int(bands.get("E", 0)),
                "N": int(bands.get("N", 0)),
                "total_shifts": total_shifts,
                f"Alt ({alt_band})": alt_shifts,
                "Alt %": round(100.0 * alt_shifts / total_shifts, 1)
                if total_shifts
                else 0.0,
                "hours": round(line_hours.get(employee.id, 0.0), 1),
                "target_hours": round(target, 1) if target > 0 else None,
                "weekend_shifts": weekend_scheduled,
                "weekend_target": weekend_target if weekend_target > 0 else None,
            }
        )

    tally_violations = find_portage_operational_tally_violations(
        assignments,
        period_start=period_start,
        period_end=period_end,
        template_id_to_band=template_id_to_band,
    )
    underfill = [item for item in tally_violations if item.actual < item.target]
    overfill = [item for item in tally_violations if item.actual > item.target]
    en_tally_samples = [
        f"{item.assignment_date.isoformat()} {item.band} {item.actual}/{item.target}"
        for item in tally_violations[:6]
    ]
    en_tally_underfill_samples = [
        f"{item.assignment_date.isoformat()} {item.band} {item.actual}/{item.target}"
        for item in underfill[:6]
    ]
    en_tally_overfill_samples = [
        f"{item.assignment_date.isoformat()} {item.band} {item.actual}/{item.target}"
        for item in overfill[:6]
    ]
    daily_tally_rows = build_daily_tally_rows(
        assignments,
        shift_templates,
        period_start=period_start,
        period_end=period_end,
    )

    expanded = expand_schedule_slots(
        period_start=period_start,
        period_end=period_end,
        shift_templates=shift_templates,
        concurrent_demands=portage_concurrent_demands(),
        employees=employees,
        rules=rules,
        weeks_in_period=weeks_in_period,
    )
    fill_counts = _seat_fill_counts(assignments, employees, qual_codes)
    unfilled = _unfilled_slot_breakdown(expanded, fill_counts, shift_templates)
    top_unfilled = [
        {"category": key, "count": count}
        for key, count in unfilled.most_common(12)
    ]

    return AutoPilotShiftSummary(
        total_shifts=len(assignments),
        required_slots_filled=required_slots_filled,
        required_slots_total=required_slots_total,
        open_gap_count=open_gap_count,
        slots_filled=slots_filled,
        slots_total=slots_total,
        persist_ok=persist_ok,
        cpsat_status=cpsat_status,
        post_cpsat_healing_skipped=post_cpsat_healing_skipped,
        by_band=dict(by_band),
        by_band_weekday={
            day: dict(counts) for day, counts in by_band_weekday.items()
        },
        by_role_band=dict(by_role_band),
        per_line=per_line,
        daily_tally_rows=daily_tally_rows,
        en_tally_violation_count=len(tally_violations),
        en_tally_underfill_count=len(underfill),
        en_tally_overfill_count=len(overfill),
        en_tally_samples=en_tally_samples,
        en_tally_underfill_samples=en_tally_underfill_samples,
        en_tally_overfill_samples=en_tally_overfill_samples,
        clinical_gap_count=clinical_gap_count,
        top_unfilled=top_unfilled,
        violation_codes=dict(violation_codes or {}),
    )
