"""Live draft schedule health snapshot for the manager copilot panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Mapping, Optional, Sequence, Set

import pandas as pd

from lab_scheduler.audit.compliance import ComplianceValidator
from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.policy.frame_bridge import (
    assignments_from_schedule_frame,
    normalize_grid_shift_token,
)
from lab_scheduler.scheduling.portage_equity_drift import build_portage_equity_drift_map
from lab_scheduler.scheduling.portage_equity_targets import (
    portage_alt_shift_target_for_employee,
    portage_is_fulltime_catalog_hours,
)
from lab_scheduler.scheduling.portage_template import parse_vacant_portage_line
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.rotation_spec import FT_DE_EVENING_BLOCK_DAYS
from lab_scheduler.scheduling.weekend_placement_rules import get_grid_token
from lab_scheduler.solver.cpsat_fill import is_vacant_portage_line
from lab_scheduler.scheduling.schedule_tallies import (
    find_portage_operational_tally_violations,
    is_daily_tally_employee_id,
)

SCHEDULE_GRID_VIEW_WEEKS = 8


@dataclass(frozen=True, slots=True)
class TallyDayIssue:
    assignment_date: date
    band: str
    actual: int
    target: int
    severity: str


@dataclass(frozen=True, slots=True)
class ScheduleHealthSnapshot:
    evening_violation_days: int
    night_violation_days: int
    tally_issues: tuple[TallyDayIssue, ...]
    compliance_error_count: int
    compliance_warning_count: int
    equity_drift_lines: tuple[str, ...]
    equity_evening_mismatch_lines: tuple[str, ...]
    de_evening_pattern_lines: tuple[str, ...]
    pending_mutations: int
    hours_delta: float
    is_operational_floor_ok: bool


def chunk_index_for_date(
    dates: Sequence[date],
    target: date,
    *,
    chunk_weeks: int = SCHEDULE_GRID_VIEW_WEEKS,
) -> int:
    """Map a calendar date to the fixed grid chunk index used by the schedule view."""

    if not dates:
        return 0
    try:
        day_index = next(index for index, day in enumerate(dates) if day == target)
    except StopIteration:
        return 0
    days_per_chunk = chunk_weeks * 7
    if days_per_chunk <= 0:
        return 0
    max_chunk_index = max(0, (len(dates) - 1) // days_per_chunk)
    return min(day_index // days_per_chunk, max_chunk_index)


def format_tally_issue_message(issue: TallyDayIssue) -> str:
    band_label = "Evening" if issue.band == "E" else "Night"
    date_label = issue.assignment_date.strftime("%b %d").replace(" 0", " ")
    if issue.severity == "over":
        if issue.band == "E":
            return (
                f"{date_label} {band_label} {issue.actual}/{issue.target} — "
                f"need 2 total (1 MLT + 1 MLA); you have {issue.actual}."
            )
        return (
            f"{date_label} {band_label} {issue.actual}/{issue.target} — "
            f"need 2 total; you have {issue.actual}."
        )
    shortfall = issue.target - issue.actual
    seat_word = "evening" if issue.band == "E" else "night"
    return (
        f"{date_label} {band_label} {issue.actual}/{issue.target} — "
        f"short {shortfall} {seat_word} seat{'s' if shortfall != 1 else ''}."
    )


def _template_id_to_band(
    template_info: Mapping[str, ShiftTemplateInfo],
) -> Dict[str, str]:
    bands: Dict[str, str] = {}
    for template_id, template in template_info.items():
        code = str(template.code or "").strip().upper()
        if code in {"MORNING", "M", "DAY"}:
            bands[str(template_id)] = "D"
        elif code == "EVENING":
            bands[str(template_id)] = "E"
        elif code == "NIGHT":
            bands[str(template_id)] = "N"
    return bands


def _shift_mix_from_row(
    row: Mapping[str, object],
    date_keys: Sequence[str],
) -> Dict[str, int]:
    counts = {"D": 0, "E": 0, "N": 0, "weekend": 0}
    for key in date_keys:
        band = normalize_grid_shift_token(row.get(key, ""))
        if band not in {"D", "E", "N"}:
            continue
        counts[band] += 1
        try:
            day = date.fromisoformat(key)
        except ValueError:
            continue
        if day.weekday() >= 5:
            counts["weekend"] += 1
    return counts


def _employee_grid_shift_stats(
    row: Mapping[str, object],
    date_keys: Sequence[str],
    contract_line_type: object,
) -> Optional[Dict[str, int]]:
    counts = _shift_mix_from_row(row, date_keys)
    contract = str(contract_line_type or "D/E").strip().upper()
    if contract == "D/N":
        total = counts["D"] + counts["N"]
        alternate = counts["N"]
    elif contract == "D/E":
        total = counts["D"] + counts["E"]
        alternate = counts["E"]
    else:
        total = counts["D"] + counts["E"] + counts["N"]
        alternate = counts["E"] + counts["N"]
    if total <= 0:
        return None
    return {
        "alternate_shifts": alternate,
        "total_shifts": total,
        "weekend_shifts": counts["weekend"],
    }


def _issues_from_violations(
    violations: Sequence[object],
) -> tuple[TallyDayIssue, ...]:
    issues: list[TallyDayIssue] = []
    for violation in violations:
        actual = int(getattr(violation, "actual", 0))
        target = int(getattr(violation, "target", 0))
        issues.append(
            TallyDayIssue(
                assignment_date=getattr(violation, "assignment_date"),
                band=str(getattr(violation, "band", "")),
                actual=actual,
                target=target,
                severity="under" if actual < target else "over",
            )
        )

    def _sort_key(item: TallyDayIssue) -> tuple:
        severity_rank = 0 if item.severity == "over" else 1
        band_rank = 0 if item.band == "E" else 1
        return (severity_rank, item.assignment_date, band_rank)

    return tuple(sorted(issues, key=_sort_key))


def _build_equity_summaries(
    *,
    schedule_frame: pd.DataFrame,
    employees: Sequence[Mapping[str, object] | EmployeeProfile],
    dates: Sequence[date],
    catalog_targets: Mapping[str, float],
    qual_codes: Mapping[str, str],
    period_start: date,
    period_end: date,
    emp_quals: Mapping[str, Set[str]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    date_keys = [day.isoformat() for day in dates]
    profiles: list[EmployeeProfile] = []
    profile_by_id: Dict[str, EmployeeProfile] = {}
    for employee in employees:
        if isinstance(employee, EmployeeProfile):
            profile = employee
        else:
            employee_id = str(employee["id"])
            profile = EmployeeProfile(
                id=employee_id,
                full_name=str(employee.get("full_name") or employee.get("Employee") or ""),
                fte=float(employee.get("fte") or 1.0),
                qualification_ids=emp_quals.get(employee_id, set()),
                contract_line_type=employee.get("contract_line_type"),
            )
        profiles.append(profile)
        profile_by_id[profile.id] = profile

    alt_by_id: Dict[str, int] = {}
    total_by_id: Dict[str, int] = {}
    wknd_by_id: Dict[str, int] = {}
    for _, matrix_row in schedule_frame.iterrows():
        employee_id = str(matrix_row.get("employee_id", "") or "")
        if not employee_id or is_daily_tally_employee_id(employee_id):
            continue
        stats = _employee_grid_shift_stats(
            matrix_row,
            date_keys,
            matrix_row.get("contract_line_type", "D/E"),
        )
        if stats is None:
            continue
        alt_by_id[employee_id] = int(stats["alternate_shifts"])
        total_by_id[employee_id] = int(stats["total_shifts"])
        wknd_by_id[employee_id] = int(stats["weekend_shifts"])

    drift_map = build_portage_equity_drift_map(
        [profile_by_id[eid] for eid in alt_by_id if eid in profile_by_id],
        catalog_targets,
        alternate_shifts_by_employee=alt_by_id,
        total_shifts_by_employee=total_by_id,
        weekend_shifts_by_employee=wknd_by_id,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
    )

    drift_lines: list[str] = []
    evening_mismatch_lines: list[str] = []
    for employee_id, drift in drift_map.items():
        profile = profile_by_id.get(employee_id)
        if profile is None:
            continue
        if drift.has_drift:
            drift_lines.append(profile.full_name)
        contract = (profile.contract_line_type or "").upper()
        vacant = parse_vacant_portage_line(profile.full_name)
        catalog_hours = float(catalog_targets.get(employee_id, 0.0))
        if (
            vacant is not None
            and contract == "D/E"
            and portage_is_fulltime_catalog_hours(catalog_hours)
        ):
            alt_target = portage_alt_shift_target_for_employee(profile, catalog_hours)
            evening_count = int(alt_by_id.get(employee_id, 0))
            if evening_count != alt_target:
                evening_mismatch_lines.append(
                    f"{profile.full_name} ({evening_count} E, target {alt_target})"
                )

    return tuple(drift_lines), tuple(evening_mismatch_lines)


def _max_consecutive_e_run(
    row_idx: int,
    frame: pd.DataFrame,
    dates: Sequence[date],
) -> int:
    best = 0
    current = 0
    for day in dates:
        if get_grid_token(frame, row_idx, day) == "E":
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _build_de_evening_pattern_lines(
    *,
    schedule_frame: pd.DataFrame,
    dates: Sequence[date],
    employees: Sequence[Mapping[str, object] | EmployeeProfile],
    catalog_targets: Mapping[str, float],
) -> tuple[str, ...]:
    date_keys = [day.isoformat() for day in dates]
    row_by_id: Dict[str, int] = {}
    for index, row in schedule_frame.iterrows():
        employee_id = str(row.get("employee_id", "") or "")
        if employee_id and not is_daily_tally_employee_id(employee_id):
            row_by_id[employee_id] = int(index)

    lines: list[str] = []
    for employee in employees:
        if isinstance(employee, EmployeeProfile):
            profile = employee
        else:
            profile = EmployeeProfile(
                id=str(employee["id"]),
                full_name=str(employee.get("full_name") or ""),
                fte=float(employee.get("fte") or 1.0),
                qualification_ids=set(),
                contract_line_type=employee.get("contract_line_type"),
            )
        if not is_vacant_portage_line(profile.full_name):
            continue
        if (profile.contract_line_type or "").upper() != "D/E":
            continue
        if not portage_is_fulltime_catalog_hours(float(catalog_targets.get(profile.id, 0.0))):
            continue
        row_idx = row_by_id.get(profile.id)
        if row_idx is None:
            continue
        matrix_row = schedule_frame.iloc[row_idx]
        e_count = sum(
            1
            for key in date_keys
            if get_grid_token(schedule_frame, row_idx, date.fromisoformat(key)) == "E"
        )
        alt_target = portage_alt_shift_target_for_employee(
            profile, float(catalog_targets.get(profile.id, 0.0))
        )
        if e_count != alt_target:
            lines.append(f"{profile.full_name}: {e_count} E (expected {alt_target})")
        max_run = _max_consecutive_e_run(row_idx, schedule_frame, dates)
        if max_run < FT_DE_EVENING_BLOCK_DAYS:
            lines.append(
                f"{profile.full_name}: max E run {max_run} (expected {FT_DE_EVENING_BLOCK_DAYS})"
            )
    return tuple(lines[:8])


def build_schedule_health_snapshot(
    *,
    schedule_frame: pd.DataFrame,
    employees: Sequence[Mapping[str, object] | EmployeeProfile],
    dates: Sequence[date],
    templates: Mapping[str, Mapping[str, object]],
    template_info: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    qual_codes: Mapping[str, str],
    pending_mutations: int,
    hours_delta: float,
    rules: JurisdictionRules,
    weeks_in_period: int,
    employee_target_hours: Mapping[str, float],
    emp_quals: Optional[Mapping[str, Set[str]]] = None,
) -> ScheduleHealthSnapshot:
    scheduled = assignments_from_schedule_frame(
        schedule_frame,
        employees=employees,
        dates=dates,
        templates=templates,
    )
    template_id_to_band = _template_id_to_band(template_info)
    violations = find_portage_operational_tally_violations(
        scheduled,
        period_start=period_start,
        period_end=period_end,
        template_id_to_band=template_id_to_band,
    )
    tally_issues = _issues_from_violations(violations)
    evening_dates = {item.assignment_date for item in tally_issues if item.band == "E"}
    night_dates = {item.assignment_date for item in tally_issues if item.band == "N"}

    profiles: list[EmployeeProfile] = []
    for employee in employees:
        if isinstance(employee, EmployeeProfile):
            profiles.append(employee)
        else:
            employee_id = str(employee["id"])
            profiles.append(
                EmployeeProfile(
                    id=employee_id,
                    full_name=str(employee.get("full_name") or ""),
                    fte=float(employee.get("fte") or 1.0),
                    qualification_ids=(emp_quals or {}).get(employee_id, set()),
                    contract_line_type=employee.get("contract_line_type"),
                )
            )

    compliance_result = ComplianceValidator().validate(
        rules=rules,
        employees=profiles,
        assignments=scheduled,
        shift_templates=template_info,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
        log_rejections=False,
    )

    equity_drift_lines, equity_evening_mismatch_lines = _build_equity_summaries(
        schedule_frame=schedule_frame,
        employees=employees,
        dates=dates,
        catalog_targets=employee_target_hours,
        qual_codes=qual_codes,
        period_start=period_start,
        period_end=period_end,
        emp_quals=emp_quals or {},
    )
    de_pattern_lines = _build_de_evening_pattern_lines(
        schedule_frame=schedule_frame,
        dates=dates,
        employees=employees,
        catalog_targets=employee_target_hours,
    )

    return ScheduleHealthSnapshot(
        evening_violation_days=len(evening_dates),
        night_violation_days=len(night_dates),
        tally_issues=tally_issues,
        compliance_error_count=compliance_result.conflict_count,
        compliance_warning_count=len(compliance_result.warnings),
        equity_drift_lines=equity_drift_lines,
        equity_evening_mismatch_lines=equity_evening_mismatch_lines,
        de_evening_pattern_lines=de_pattern_lines,
        pending_mutations=pending_mutations,
        hours_delta=hours_delta,
        is_operational_floor_ok=not tally_issues,
    )
