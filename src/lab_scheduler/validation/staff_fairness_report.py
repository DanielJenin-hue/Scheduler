"""
Staff Fairness & Burnout Report — read-only evaluation layer for manager review.

Pre-deployment checklist (run before first live breakroom post):
  1. Run Auto-Pilot on the target period; confirm ``overall_status`` in this report.
  2. If REVIEW_REQUIRED or NOT_RECOMMENDED: walk the flag table, attestation note, audit log.
  3. Verify named employees appear (not only Vacant Line placeholders).
  4. Cross-check vacant-line pool equity vs Auto-Pilot ``shift_equity_metrics`` expander.
  5. Confirm breakroom HTML has no staff-visible fairness section (manager-only v1).
  6. Run compliance audit export separately — fairness complements, does not replace legal rest.
  7. Archive Schedule Export JSON + fairness HTML with the period record.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.compliance.engine import (
    ComplianceReport,
    ShiftTemplateInfo,
    _consecutive_work_day_streaks,
)
from lab_scheduler.engine.demand import (
    PORTAGE_MAX_CONSECUTIVE_WORK_DAYS,
    PORTAGE_MIN_INTER_BLOCK_REST_DAYS,
    find_day_night_transition_violations,
)
from lab_scheduler.scheduling.fairness_thresholds import (
    DEFAULT_FAIRNESS_THRESHOLDS,
    FairnessThresholds,
)
from lab_scheduler.scheduling.portage_equity_targets import (
    build_vacant_line_weekend_target_map,
    portage_alt_shift_target,
    portage_alt_shift_target_for_employee,
    portage_is_parttime_catalog_hours,
    portage_weekend_shift_target,
)
from lab_scheduler.scheduling.portage_equity_drift import evaluate_portage_equity_drift
from lab_scheduler.scheduling.auto_generate import PlannedAssignment, infer_qual_code
from lab_scheduler.scheduling.night_streak_corrector import (
    PORTAGE_MAX_CONSECUTIVE_NIGHTS,
    find_consecutive_night_streaks,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.streak_validator import (
    find_work_streak_violations_for_dates,
)
from lab_scheduler.solver.cpsat_fill import (
    FULLTIME_ACTIVE_WEEKENDS_REQUIRED,
    FULLTIME_PERIOD_TARGET_HOURS,
    _band_counts_for_employee,
    compute_employee_alternate_shift_share,
    compute_pool_equity_for_all_employees,
    shift_code_to_band,
)

SEVERITY_RED = "RED"
SEVERITY_YELLOW = "YELLOW"
SEVERITY_INFO = "INFO"

STATUS_READY = "READY"
STATUS_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATUS_NOT_RECOMMENDED = "NOT_RECOMMENDED"

WORK_SHIFT_CODES = frozenset({"MORNING", "EVENING", "NIGHT"})


@dataclass(frozen=True, slots=True)
class FairnessFlag:
    employee_id: str
    employee_name: str
    code: str
    severity: str
    message: str
    metric_value: Optional[float] = None
    threshold: Optional[float] = None
    peer_pool: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "metric_value": self.metric_value,
            "threshold": self.threshold,
            "peer_pool": self.peer_pool,
        }


@dataclass(frozen=True, slots=True)
class EmployeeFairnessRow:
    employee_id: str
    employee_name: str
    peer_pool: str
    contract_line_type: str
    target_hours: float
    scheduled_hours: float
    total_d: int
    total_e: int
    total_n: int
    alternate_shifts: int
    pool_avg_alternate_shifts: Optional[float]
    alternate_shift_delta: Optional[int]
    active_weekends: int
    weekend_shift_count: int
    max_consecutive_work_days: int
    max_consecutive_nights: int
    max_evenings_in_window: int
    flags: Tuple[FairnessFlag, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "employee_id": self.employee_id,
            "employee_name": self.employee_name,
            "peer_pool": self.peer_pool,
            "contract_line_type": self.contract_line_type,
            "target_hours": self.target_hours,
            "scheduled_hours": self.scheduled_hours,
            "total_d": self.total_d,
            "total_e": self.total_e,
            "total_n": self.total_n,
            "alternate_shifts": self.alternate_shifts,
            "pool_avg_alternate_shifts": self.pool_avg_alternate_shifts,
            "alternate_shift_delta": self.alternate_shift_delta,
            "active_weekends": self.active_weekends,
            "weekend_shift_count": self.weekend_shift_count,
            "max_consecutive_work_days": self.max_consecutive_work_days,
            "max_consecutive_nights": self.max_consecutive_nights,
            "max_evenings_in_window": self.max_evenings_in_window,
            "flags": [flag.to_dict() for flag in self.flags],
        }


@dataclass(frozen=True, slots=True)
class PoolFairnessSummary:
    pool_name: str
    member_count: int
    target_avg_alternate_shifts: Optional[int]
    pool_avg_alternate_shift_pct: Optional[float]
    weekend_floor_average: int

    def to_dict(self) -> Dict[str, object]:
        return {
            "pool_name": self.pool_name,
            "member_count": self.member_count,
            "target_avg_alternate_shifts": self.target_avg_alternate_shifts,
            "pool_avg_alternate_shift_pct": self.pool_avg_alternate_shift_pct,
            "weekend_floor_average": self.weekend_floor_average,
        }


@dataclass(frozen=True, slots=True)
class StaffFairnessReport:
    report_id: str
    generated_at_utc: str
    tenant_name: str
    period_name: str
    period_start: date
    period_end: date
    overall_status: str
    attestation_required: bool
    red_flag_count: int
    yellow_flag_count: int
    employee_rows: Tuple[EmployeeFairnessRow, ...]
    pool_summaries: Tuple[PoolFairnessSummary, ...]
    flags: Tuple[FairnessFlag, ...]
    content_hash: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "report_id": self.report_id,
            "generated_at_utc": self.generated_at_utc,
            "tenant_name": self.tenant_name,
            "period_name": self.period_name,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "overall_status": self.overall_status,
            "attestation_required": self.attestation_required,
            "red_flag_count": self.red_flag_count,
            "yellow_flag_count": self.yellow_flag_count,
            "employee_rows": [row.to_dict() for row in self.employee_rows],
            "pool_summaries": [summary.to_dict() for summary in self.pool_summaries],
            "flags": [flag.to_dict() for flag in self.flags],
            "content_hash": self.content_hash,
        }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _esc(text: object) -> str:
    return html.escape(str(text))


def _attr(obj: Mapping[str, object], key: str, default: object = "") -> object:
    return obj.get(key, default)


def _employee_profiles(
    employees: Sequence[Mapping[str, object]],
    qual_lookup: Mapping[str, str],
) -> List[EmployeeProfile]:
    profiles: List[EmployeeProfile] = []
    for employee in employees:
        employee_id = str(_attr(employee, "id"))
        profiles.append(
            EmployeeProfile(
                id=employee_id,
                full_name=str(_attr(employee, "full_name")),
                fte=float(_attr(employee, "fte", 1.0) or 1.0),
                qualification_ids=set(),
                contract_line_type=str(_attr(employee, "contract_line_type") or "") or None,
            )
        )
    return profiles


def _shift_templates_for_engine(
    templates: Mapping[str, Mapping[str, object]],
) -> Dict[str, ShiftTemplateInfo]:
    return {
        tid: ShiftTemplateInfo(
            id=tid,
            code=str(t.get("code", "")),
            name=str(t.get("name", t.get("code", tid))),
            start_time=str(t.get("start_time", "08:00")),
            end_time=str(t.get("end_time", "16:00")),
            duration_minutes=int(t.get("duration_minutes", 480)),
            crosses_midnight=bool(t.get("crosses_midnight", False)),
        )
        for tid, t in templates.items()
    }


def _planned_assignments(
    assignments: Sequence[Mapping[str, object]],
) -> List[PlannedAssignment]:
    rows: List[PlannedAssignment] = []
    for assignment in assignments:
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        rows.append(
            PlannedAssignment(
                employee_id=str(assignment["employee_id"]),
                shift_template_id=str(assignment["shift_template_id"]),
                assignment_date=assignment_date,
            )
        )
    return rows


def _assignment_triples(
    assignments: Sequence[Mapping[str, object]],
) -> List[Tuple[str, date, str]]:
    triples: List[Tuple[str, date, str]] = []
    for assignment in assignments:
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        triples.append(
            (
                str(assignment["employee_id"]),
                assignment_date,
                str(assignment["shift_template_id"]),
            )
        )
    return triples


def _bands_by_date(
    employee_id: str,
    assignments: Sequence[Mapping[str, object]],
    shift_templates: Mapping[str, Mapping[str, object]],
) -> Dict[date, str]:
    bands: Dict[date, str] = {}
    for assignment in assignments:
        if str(_attr(assignment, "employee_id")) != employee_id:
            continue
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        template = shift_templates.get(str(_attr(assignment, "shift_template_id")))
        if template is None:
            continue
        band = shift_code_to_band(str(template.get("code", "")))
        if band:
            bands[assignment_date] = band
    return bands


def _work_dates(
    employee_id: str,
    assignments: Sequence[Mapping[str, object]],
    shift_templates: Mapping[str, Mapping[str, object]],
) -> Set[date]:
    dates: Set[date] = set()
    for assignment in assignments:
        if str(_attr(assignment, "employee_id")) != employee_id:
            continue
        template = shift_templates.get(str(_attr(assignment, "shift_template_id")))
        if template is None:
            continue
        if str(template.get("code", "")) not in WORK_SHIFT_CODES:
            continue
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        dates.add(assignment_date)
    return dates


def _max_consecutive_work_days(work_dates: Set[date]) -> int:
    if not work_dates:
        return 0
    return max(length for _start, _end, length in _consecutive_work_day_streaks(sorted(work_dates)))


def _max_consecutive_nights(
    employee_id: str,
    *,
    period_start: date,
    period_end: date,
    planned: Sequence[PlannedAssignment],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> int:
    streaks = find_consecutive_night_streaks(
        employee_id=employee_id,
        period_start=period_start,
        period_end=period_end,
        assignments=planned,
        shift_templates=shift_templates,
        min_length=1,
    )
    return max((streak.length for streak in streaks), default=0)


def _count_active_weekends(
    bands_by_date: Mapping[date, str],
    period_start: date,
    period_end: date,
) -> int:
    active = 0
    current = period_start
    while current <= period_end:
        if current.weekday() == 5:
            sunday = current + timedelta(days=1)
            if current in bands_by_date or (sunday <= period_end and sunday in bands_by_date):
                active += 1
        current += timedelta(days=1)
    return active


def _weekend_shift_count(bands_by_date: Mapping[date, str]) -> int:
    return sum(1 for day, _band in bands_by_date.items() if day.weekday() >= 5)


def _max_evenings_in_rolling_window(
    bands_by_date: Mapping[date, str],
    *,
    window_days: int,
    period_start: date,
    period_end: date,
) -> int:
    evening_dates = sorted(day for day, band in bands_by_date.items() if band == "E")
    if not evening_dates:
        return 0
    max_count = 0
    for index, start_day in enumerate(evening_dates):
        window_end = start_day + timedelta(days=window_days - 1)
        count = 0
        for day in evening_dates[index:]:
            if day > window_end:
                break
            count += 1
        max_count = max(max_count, count)
    return max_count


def _find_ed_clopening_violations(
    employee_id: str,
    employee_name: str,
    bands_by_date: Mapping[date, str],
    *,
    peer_pool: str,
) -> List[FairnessFlag]:
    flags: List[FairnessFlag] = []
    for work_date, band in bands_by_date.items():
        if band != "E":
            continue
        next_day = work_date + timedelta(days=1)
        if bands_by_date.get(next_day) == "D":
            flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="E_TO_D_CLOOPEN",
                    severity=SEVERITY_RED,
                    message=(
                        f"{employee_name}: Evening on {work_date.isoformat()} followed by "
                        f"Day on {next_day.isoformat()} (clopening)."
                    ),
                    peer_pool=peer_pool,
                )
            )
    return flags


def _find_post_night_recovery_violations(
    employee_id: str,
    employee_name: str,
    bands_by_date: Mapping[date, str],
    *,
    peer_pool: str,
    required_off_days: int,
) -> List[FairnessFlag]:
    flags: List[FairnessFlag] = []
    sorted_days = sorted(bands_by_date)
    run_end: Optional[date] = None
    for day in sorted_days:
        if bands_by_date[day] == "N":
            run_end = day
            continue
        if run_end is None or bands_by_date[day] != "D":
            continue
        off_days = (day - run_end).days - 1
        if off_days < required_off_days:
            flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="POST_NIGHT_RECOVERY",
                    severity=SEVERITY_YELLOW,
                    message=(
                        f"{employee_name}: Day shift on {day.isoformat()} only {off_days} "
                        f"calendar day(s) after night block ending {run_end.isoformat()} "
                        f"(need {required_off_days} off days)."
                    ),
                    metric_value=float(off_days),
                    threshold=float(required_off_days),
                    peer_pool=peer_pool,
                )
            )
        run_end = None
    return flags


def _find_inter_block_rest_violations(
    employee_id: str,
    employee_name: str,
    work_dates: Set[date],
    *,
    peer_pool: str,
    min_rest_days: int = PORTAGE_MIN_INTER_BLOCK_REST_DAYS,
) -> List[FairnessFlag]:
    flags: List[FairnessFlag] = []
    if len(work_dates) < 2:
        return flags
    sorted_dates = sorted(work_dates)
    for index in range(len(sorted_dates) - 1):
        left = sorted_dates[index]
        right = sorted_dates[index + 1]
        if (right - left).days <= 1:
            continue
        off_days = (right - left).days - 1
        if off_days >= min_rest_days:
            continue
        flags.append(
            FairnessFlag(
                employee_id=employee_id,
                employee_name=employee_name,
                code="INTER_BLOCK_REST",
                severity=SEVERITY_YELLOW,
                message=(
                    f"{employee_name}: Only {off_days} day(s) off between work blocks "
                    f"({left.isoformat()} .. {right.isoformat()}); "
                    f"Portage policy expects {min_rest_days}."
                ),
                metric_value=float(off_days),
                threshold=float(min_rest_days),
                peer_pool=peer_pool,
            )
        )
    return flags


def _pool_name_for_employee(
    employee: Mapping[str, object],
    qual_lookup: Mapping[str, str],
) -> str:
    profile = EmployeeProfile(
        id=str(_attr(employee, "id")),
        full_name=str(_attr(employee, "full_name")),
        fte=float(_attr(employee, "fte", 1.0) or 1.0),
        qualification_ids=set(),
        contract_line_type=str(_attr(employee, "contract_line_type") or "") or None,
    )
    qual_code = qual_lookup.get(profile.id, infer_qual_code(profile))
    contract = str(_attr(employee, "contract_line_type") or "D/E").replace("/", "_")
    return f"{qual_code.upper()}_{contract}_Pool"


def _is_fulltime_target_hours(target_hours: float) -> bool:
    return target_hours >= FULLTIME_PERIOD_TARGET_HOURS - 8


def _proportional_weekend_target_map(
    employees: Sequence[Mapping[str, object]],
    target_hours: Mapping[str, float],
    qual_lookup: Mapping[str, str],
    *,
    period_start: date,
    period_end: date,
) -> Dict[str, int]:
    from lab_scheduler.scheduling.portage_feasibility import portage_qual_contract_weekend_targets_from_stamps
    from lab_scheduler.scheduling.portage_template import (
        parse_vacant_portage_line,
        vacant_master_catalog_period_weekend_shifts,
    )
    from lab_scheduler.scheduling.portage_equity_targets import portage_weekend_shift_target
    from lab_scheduler.scheduling.profiles import EmployeeProfile

    weekend_day_count = sum(
        1
        for offset in range((period_end - period_start).days + 1)
        if date.fromordinal(period_start.toordinal() + offset).weekday() >= 5
    )
    if weekend_day_count <= 0:
        return {}

    groups: Dict[Tuple[str, str], List[Tuple[str, int]]] = {}
    for employee in employees:
        employee_id = str(_attr(employee, "id"))
        if parse_vacant_portage_line(str(_attr(employee, "full_name"))) is None:
            continue
        qual = qual_lookup.get(employee_id, "MLT").upper()
        contract = str(_attr(employee, "contract_line_type") or "D/E").upper()
        catalog_hours = float(target_hours.get(employee_id, 0.0))
        profile = EmployeeProfile(
            id=employee_id,
            full_name=str(_attr(employee, "full_name")),
            fte=float(_attr(employee, "fte", 1.0) or 1.0),
            qualification_ids=set(),
            contract_line_type=str(_attr(employee, "contract_line_type") or "") or None,
        )
        stamped = vacant_master_catalog_period_weekend_shifts(
            profile,
            period_start,
            period_end,
        )
        ideal = stamped if stamped > 0 else portage_weekend_shift_target(catalog_hours)
        groups.setdefault((qual, contract), []).append((employee_id, ideal))

    proportional: Dict[str, int] = {}
    for (qual, _contract), members in groups.items():
        employee_ids = [employee_id for employee_id, _ideal in members]
        ideals = [ideal for _employee_id, ideal in members]
        scaled = portage_qual_contract_weekend_targets_from_stamps(
            ideals,
            qual_code=qual,
            weekend_day_count=weekend_day_count,
        )
        for employee_id, target in zip(employee_ids, scaled, strict=True):
            proportional[employee_id] = target
    return proportional


def _resolve_overall_status(flags: Sequence[FairnessFlag]) -> str:
    if any(flag.severity == SEVERITY_RED for flag in flags):
        return STATUS_NOT_RECOMMENDED
    if any(flag.severity == SEVERITY_YELLOW for flag in flags):
        return STATUS_REVIEW_REQUIRED
    return STATUS_READY


def staff_fairness_export_allowed(
    report: StaffFairnessReport | Mapping[str, object],
    *,
    attested: bool,
) -> bool:
    """Breakroom export is never blocked by fairness findings (advisory-only v1)."""

    del report, attested
    return True


def record_staff_fairness_attestation(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    manager_id: str,
    schedule_period_id: str,
    report: StaffFairnessReport,
    note: str = "",
) -> int:
    _ensure_sys_audit_log_for_fairness(conn)
    metadata = {
        "schedule_period_id": schedule_period_id,
        "report_id": report.report_id,
        "content_hash": report.content_hash,
        "overall_status": report.overall_status,
        "red_flag_count": report.red_flag_count,
        "yellow_flag_count": report.yellow_flag_count,
        "manager_note": note.strip(),
        "flag_codes": sorted({flag.code for flag in report.flags}),
    }
    cur = conn.execute(
        """
        INSERT INTO sys_audit_log (
          recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        )
        VALUES (?, ?, ?, 'staff_fairness_attestation', NULL, 0, ?)
        """,
        (_utc_now_iso(), tenant_id, manager_id, json.dumps(metadata, sort_keys=True)),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _ensure_staff_fairness_attestation_action_type(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'sys_audit_log'"
    ).fetchone()
    ddl = (row[0] or "") if row else ""
    if ddl and "'staff_fairness_attestation'" in ddl:
        return
    conn.executescript(
        """
        CREATE TABLE sys_audit_log__fairness (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          recorded_at_utc TEXT NOT NULL,
          tenant_id TEXT NOT NULL,
          manager_id TEXT NOT NULL,
          action_type TEXT NOT NULL,
          employee_id TEXT,
          shifts_vacated_count INTEGER NOT NULL DEFAULT 0,
          metadata_json TEXT,
          CHECK (action_type IN (
            'employee_deactivation',
            'employee_reactivation',
            'audit_warning',
            'snapshot_restore',
            'staff_fairness_attestation'
          ))
        );

        INSERT INTO sys_audit_log__fairness (
          id, recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        )
        SELECT
          id, recorded_at_utc, tenant_id, manager_id, action_type,
          employee_id, shifts_vacated_count, metadata_json
        FROM sys_audit_log;

        DROP TABLE sys_audit_log;
        ALTER TABLE sys_audit_log__fairness RENAME TO sys_audit_log;

        CREATE INDEX IF NOT EXISTS idx_sys_audit_log_tenant_recorded
          ON sys_audit_log (tenant_id, recorded_at_utc DESC);
        """
    )


def _ensure_sys_audit_log_for_fairness(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='sys_audit_log'"
    ).fetchone()
    if row is None:
        conn.execute(
            """
            CREATE TABLE sys_audit_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              recorded_at_utc TEXT NOT NULL,
              tenant_id TEXT NOT NULL,
              manager_id TEXT NOT NULL,
              action_type TEXT NOT NULL,
              employee_id TEXT,
              shifts_vacated_count INTEGER NOT NULL DEFAULT 0,
              metadata_json TEXT,
              CHECK (action_type IN (
                'employee_deactivation',
                'employee_reactivation',
                'audit_warning',
                'snapshot_restore',
                'staff_fairness_attestation'
              ))
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sys_audit_log_tenant_recorded
              ON sys_audit_log (tenant_id, recorded_at_utc DESC)
            """
        )
        conn.commit()
        return
    _ensure_staff_fairness_attestation_action_type(conn)


def build_staff_fairness_report(
    *,
    tenant_name: str,
    period_name: str,
    period_start: date,
    period_end: date,
    employees: Sequence[Mapping[str, object]],
    assignments: Sequence[Mapping[str, object]],
    shift_templates: Mapping[str, Mapping[str, object]],
    target_hours: Mapping[str, float],
    qual_lookup: Optional[Mapping[str, str]] = None,
    compliance_report: Optional[ComplianceReport] = None,
    thresholds: FairnessThresholds = DEFAULT_FAIRNESS_THRESHOLDS,
) -> StaffFairnessReport:
    qual_lookup = dict(qual_lookup or {})
    template_info = _shift_templates_for_engine(shift_templates)
    planned = _planned_assignments(assignments)
    profiles = _employee_profiles(employees, qual_lookup)
    profile_by_id = {profile.id: profile for profile in profiles}
    pool_equity = compute_pool_equity_for_all_employees(
        profiles,
        assignments,
        shift_templates=shift_templates,
        qual_lookup=qual_lookup,
    )

    pool_alt_targets: Dict[str, int] = {}
    pool_alt_pcts: Dict[str, float] = {}
    for pool_name, payload in pool_equity.items():
        if not isinstance(payload, dict):
            continue
        if "target_avg_evenings" in payload:
            pool_alt_targets[pool_name] = int(payload["target_avg_evenings"])
        elif "target_avg_nights" in payload:
            pool_alt_targets[pool_name] = int(payload["target_avg_nights"])
        avg_pct = payload.get("pool_avg_alternate_shift_pct")
        if avg_pct is not None:
            pool_alt_pcts[pool_name] = float(avg_pct)

    weekend_counts = [
        _weekend_shift_count(_bands_by_date(str(_attr(employee, "id")), assignments, shift_templates))
        for employee in employees
        if _work_dates(str(_attr(employee, "id")), assignments, shift_templates)
    ]
    weekend_floor = math.floor(sum(weekend_counts) / len(weekend_counts)) if weekend_counts else 0
    proportional_weekend_targets = _proportional_weekend_target_map(
        employees,
        target_hours,
        qual_lookup,
        period_start=period_start,
        period_end=period_end,
    )
    portage_weekend_targets = build_vacant_line_weekend_target_map(
        profiles,
        target_hours,
        qual_lookup,
        period_start=period_start,
        period_end=period_end,
    )

    hours_by_employee: Dict[str, float] = {}
    if compliance_report is not None:
        for summary in compliance_report.labor_summaries:
            hours_by_employee[summary.employee_id] = float(summary.scheduled_hours)

    transition_violations = find_day_night_transition_violations(
        _assignment_triples(assignments),
        template_info,
    )
    transition_by_employee: Dict[str, List[Tuple[date, date]]] = defaultdict(list)
    for employee_id, day_d, day_n in transition_violations:
        transition_by_employee[employee_id].append((day_d, day_n))

    all_flags: List[FairnessFlag] = []
    employee_rows: List[EmployeeFairnessRow] = []

    for employee in employees:
        employee_id = str(_attr(employee, "id"))
        employee_name = str(_attr(employee, "full_name"))
        contract_line_type = str(_attr(employee, "contract_line_type") or "")
        peer_pool = _pool_name_for_employee(employee, qual_lookup)
        target = float(target_hours.get(employee_id, 0.0))
        bands = _bands_by_date(employee_id, assignments, shift_templates)
        work_dates = _work_dates(employee_id, assignments, shift_templates)
        counts = _band_counts_for_employee(employee_id, assignments, shift_templates)
        share = compute_employee_alternate_shift_share(
            employee_id,
            contract_line_type=contract_line_type,
            assignments=assignments,
            shift_templates=shift_templates,
        )
        alternate_shifts = int(share["alternate_shifts"]) if share else 0
        pool_avg_alt = pool_alt_targets.get(peer_pool)
        alt_delta: Optional[int] = None
        if pool_avg_alt is not None:
            alt_delta = alternate_shifts - pool_avg_alt

        row_flags: List[FairnessFlag] = []

        for violation in find_work_streak_violations_for_dates(
            employee_id=employee_id,
            employee_name=employee_name,
            work_dates=work_dates,
            period_start=period_start,
            period_end=period_end,
        ):
            row_flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="WORK_STREAK",
                    severity=SEVERITY_RED,
                    message=violation.message,
                    metric_value=float(violation.length),
                    threshold=float(PORTAGE_MAX_CONSECUTIVE_WORK_DAYS),
                    peer_pool=peer_pool,
                )
            )

        max_nights = _max_consecutive_nights(
            employee_id,
            period_start=period_start,
            period_end=period_end,
            planned=planned,
            shift_templates=template_info,
        )
        if max_nights > PORTAGE_MAX_CONSECUTIVE_NIGHTS:
            row_flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="NIGHT_STREAK",
                    severity=SEVERITY_RED,
                    message=(
                        f"{employee_name}: {max_nights} consecutive night shifts exceeds "
                        f"{PORTAGE_MAX_CONSECUTIVE_NIGHTS}-night limit."
                    ),
                    metric_value=float(max_nights),
                    threshold=float(PORTAGE_MAX_CONSECUTIVE_NIGHTS),
                    peer_pool=peer_pool,
                )
            )

        for day_d, day_n in transition_by_employee.get(employee_id, ()):
            row_flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="D_TO_N_TRANSITION",
                    severity=SEVERITY_RED,
                    message=(
                        f"{employee_name}: Day on {day_d.isoformat()} followed by "
                        f"Night on {day_n.isoformat()} (transition burnout risk)."
                    ),
                    peer_pool=peer_pool,
                )
            )

        row_flags.extend(
            _find_ed_clopening_violations(
                employee_id,
                employee_name,
                bands,
                peer_pool=peer_pool,
            )
        )
        row_flags.extend(
            _find_post_night_recovery_violations(
                employee_id,
                employee_name,
                bands,
                peer_pool=peer_pool,
                required_off_days=thresholds.post_night_recovery_off_days,
            )
        )
        row_flags.extend(
            _find_inter_block_rest_violations(
                employee_id,
                employee_name,
                work_dates,
                peer_pool=peer_pool,
            )
        )

        active_weekends = _count_active_weekends(bands, period_start, period_end)
        weekend_shifts = _weekend_shift_count(bands)
        profile = profile_by_id.get(employee_id)
        weekend_target = proportional_weekend_targets.get(
            employee_id,
            portage_weekend_shift_target(target),
        )
        if target >= 64.0 and employee_id in portage_weekend_targets:
            weekend_target = portage_weekend_targets[employee_id]
        active_weekend_target = max(0, int(weekend_target) // 2)
        if work_dates and weekend_target > 0:
            if abs(weekend_shifts - weekend_target) > 2:
                row_flags.append(
                    FairnessFlag(
                        employee_id=employee_id,
                        employee_name=employee_name,
                        code="WEEKEND_SHIFT_TARGET",
                        severity=SEVERITY_YELLOW,
                        message=(
                            f"{employee_name}: {weekend_shifts} weekend shifts "
                            f"(pool target is {weekend_target})."
                        ),
                        metric_value=float(weekend_shifts),
                        threshold=float(weekend_target),
                        peer_pool=peer_pool,
                    )
                )
            if _is_fulltime_target_hours(target) and active_weekends != active_weekend_target:
                row_flags.append(
                    FairnessFlag(
                        employee_id=employee_id,
                        employee_name=employee_name,
                        code="ACTIVE_WEEKENDS_LOW"
                        if active_weekends < active_weekend_target
                        else "ACTIVE_WEEKENDS_HIGH",
                        severity=SEVERITY_YELLOW,
                        message=(
                            f"{employee_name}: {active_weekends} active weekends "
                            f"(target is {active_weekend_target})."
                        ),
                        metric_value=float(active_weekends),
                        threshold=float(active_weekend_target),
                        peer_pool=peer_pool,
                    )
                )

        alt_target = (
            portage_alt_shift_target_for_employee(profile, target)
            if profile is not None
            else portage_alt_shift_target(target)
        )
        total_shifts = sum(counts.values()) if counts else 0
        catalog_drift_eligible = target >= 64.0 and (
            target >= 312.0 or portage_is_parttime_catalog_hours(target)
        )
        if profile is not None and total_shifts > 0 and catalog_drift_eligible:
            drift = evaluate_portage_equity_drift(
                profile,
                target,
                alternate_shifts=alternate_shifts,
                total_shifts=total_shifts,
                weekend_shifts=weekend_shifts,
                weekend_target=int(weekend_target),
            )
            if drift is not None and drift.alt_status == "low":
                row_flags.append(
                    FairnessFlag(
                        employee_id=employee_id,
                        employee_name=employee_name,
                        code="ALT_SHIFT_DENSITY",
                        severity=SEVERITY_YELLOW,
                        message=(
                            f"{employee_name}: {alternate_shifts} alternate shifts "
                            f"({drift.alt_density_pct:.0f}% vs {drift.alt_target_density_pct:.0f}% "
                            f"{drift.role_label} target {alt_target})."
                        ),
                        metric_value=float(alternate_shifts),
                        threshold=float(alt_target),
                        peer_pool=peer_pool,
                    )
                )
            elif drift is not None and drift.alt_status == "high":
                row_flags.append(
                    FairnessFlag(
                        employee_id=employee_id,
                        employee_name=employee_name,
                        code="ALT_SHIFT_DENSITY",
                        severity=SEVERITY_YELLOW,
                        message=(
                            f"{employee_name}: {alternate_shifts} alternate shifts "
                            f"({drift.alt_density_pct:.0f}% above {drift.alt_target_density_pct:.0f}% "
                            f"{drift.role_label} target {alt_target})."
                        ),
                        metric_value=float(alternate_shifts),
                        threshold=float(alt_target),
                        peer_pool=peer_pool,
                    )
                )
            if drift is not None and drift.weekend_status not in ("ok", "na"):
                row_flags.append(
                    FairnessFlag(
                        employee_id=employee_id,
                        employee_name=employee_name,
                        code="WEEKEND_EQUITY_DRIFT",
                        severity=SEVERITY_YELLOW,
                        message=(
                            f"{employee_name}: {weekend_shifts} weekend shifts vs pool target "
                            f"{weekend_target} ({drift.active_weekend_target} active pairs)."
                        ),
                        metric_value=float(weekend_shifts),
                        threshold=float(weekend_target),
                        peer_pool=peer_pool,
                    )
                )
        elif _is_fulltime_target_hours(target):
            if alt_target > 0 and abs(alternate_shifts - alt_target) > 1:
                row_flags.append(
                    FairnessFlag(
                        employee_id=employee_id,
                        employee_name=employee_name,
                        code="ALT_SHIFT_DENSITY",
                        severity=SEVERITY_YELLOW,
                        message=(
                            f"{employee_name}: {alternate_shifts} alternate shifts "
                            f"(FT 20% target is {alt_target} ±1)."
                        ),
                        metric_value=float(alternate_shifts),
                        threshold=float(alt_target),
                        peer_pool=peer_pool,
                    )
                )
        elif alt_target > 0 and alternate_shifts < alt_target:
            row_flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="ALT_SHIFT_UNDER_MINIMUM",
                    severity=SEVERITY_YELLOW,
                    message=(
                        f"{employee_name}: {alternate_shifts} alternate shifts "
                        f"(below minimum gap-fill target {alt_target})."
                    ),
                    metric_value=float(alternate_shifts),
                    threshold=float(alt_target),
                    peer_pool=peer_pool,
                )
            )

        if (
            _is_fulltime_target_hours(target)
            and alt_delta is not None
            and abs(alt_delta) > thresholds.alt_shift_variance_shifts
        ):
            row_flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="ALT_SHIFT_EQUITY",
                    severity=SEVERITY_YELLOW,
                    message=(
                        f"{employee_name}: {alternate_shifts} alternate shifts "
                        f"({alt_delta:+d} vs pool average {pool_avg_alt})."
                    ),
                    metric_value=float(alternate_shifts),
                    threshold=float(pool_avg_alt or 0),
                    peer_pool=peer_pool,
                )
            )

        if weekend_shifts > weekend_floor + thresholds.weekend_excess_above_floor:
            row_flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="WEEKEND_ASYMMETRY",
                    severity=SEVERITY_YELLOW,
                    message=(
                        f"{employee_name}: {weekend_shifts} weekend shifts vs pool floor "
                        f"{weekend_floor}."
                    ),
                    metric_value=float(weekend_shifts),
                    threshold=float(weekend_floor + thresholds.weekend_excess_above_floor),
                    peer_pool=peer_pool,
                )
            )

        max_evenings = _max_evenings_in_rolling_window(
            bands,
            window_days=thresholds.evening_cluster_window_days,
            period_start=period_start,
            period_end=period_end,
        )
        if max_evenings > thresholds.evening_cluster_max:
            row_flags.append(
                FairnessFlag(
                    employee_id=employee_id,
                    employee_name=employee_name,
                    code="EVENING_CLUSTER",
                    severity=SEVERITY_YELLOW,
                    message=(
                        f"{employee_name}: {max_evenings} evenings within "
                        f"{thresholds.evening_cluster_window_days}-day window "
                        f"(max recommended {thresholds.evening_cluster_max})."
                    ),
                    metric_value=float(max_evenings),
                    threshold=float(thresholds.evening_cluster_max),
                    peer_pool=peer_pool,
                )
            )

        scheduled_hours = hours_by_employee.get(employee_id, 0.0)
        if target > 0 and scheduled_hours > 0:
            hour_delta = scheduled_hours - target
            if abs(hour_delta) > thresholds.contract_hours_tolerance:
                row_flags.append(
                    FairnessFlag(
                        employee_id=employee_id,
                        employee_name=employee_name,
                        code="CONTRACT_HOURS",
                        severity=SEVERITY_YELLOW,
                        message=(
                            f"{employee_name}: {scheduled_hours:.0f}h scheduled vs "
                            f"{target:.0f}h target ({hour_delta:+.0f}h)."
                        ),
                        metric_value=scheduled_hours,
                        threshold=target,
                        peer_pool=peer_pool,
                    )
                )

        if not work_dates:
            continue

        employee_rows.append(
            EmployeeFairnessRow(
                employee_id=employee_id,
                employee_name=employee_name,
                peer_pool=peer_pool,
                contract_line_type=contract_line_type,
                target_hours=target,
                scheduled_hours=scheduled_hours,
                total_d=counts["D"],
                total_e=counts["E"],
                total_n=counts["N"],
                alternate_shifts=alternate_shifts,
                pool_avg_alternate_shifts=float(pool_avg_alt) if pool_avg_alt is not None else None,
                alternate_shift_delta=alt_delta,
                active_weekends=active_weekends,
                weekend_shift_count=weekend_shifts,
                max_consecutive_work_days=_max_consecutive_work_days(work_dates),
                max_consecutive_nights=max_nights,
                max_evenings_in_window=max_evenings,
                flags=tuple(row_flags),
            )
        )
        all_flags.extend(row_flags)

    pool_summaries: List[PoolFairnessSummary] = []
    for pool_name, payload in sorted(pool_equity.items()):
        if not isinstance(payload, dict):
            continue
        member_count = sum(
            1
            for key in payload
            if key.startswith("line_") or key.startswith("employee_")
        )
        target_alt = pool_alt_targets.get(pool_name)
        pool_summaries.append(
            PoolFairnessSummary(
                pool_name=pool_name,
                member_count=member_count,
                target_avg_alternate_shifts=target_alt,
                pool_avg_alternate_shift_pct=pool_alt_pcts.get(pool_name),
                weekend_floor_average=weekend_floor,
            )
        )

    overall_status = _resolve_overall_status(all_flags)
    red_count = sum(1 for flag in all_flags if flag.severity == SEVERITY_RED)
    yellow_count = sum(1 for flag in all_flags if flag.severity == SEVERITY_YELLOW)
    attestation_required = overall_status != STATUS_READY

    generated_at = _utc_now_iso()
    report_id = str(uuid.uuid4())
    hash_payload = (
        f"{report_id}|{generated_at}|{overall_status}|{red_count}|{yellow_count}|"
        f"{len(all_flags)}"
    )
    content_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    return StaffFairnessReport(
        report_id=report_id,
        generated_at_utc=generated_at,
        tenant_name=tenant_name,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        overall_status=overall_status,
        attestation_required=attestation_required,
        red_flag_count=red_count,
        yellow_flag_count=yellow_count,
        employee_rows=tuple(employee_rows),
        pool_summaries=tuple(pool_summaries),
        flags=tuple(all_flags),
        content_hash=content_hash,
    )


def render_staff_fairness_report_html(report: StaffFairnessReport) -> str:
    status_color = {
        STATUS_READY: "#166534",
        STATUS_REVIEW_REQUIRED: "#92400e",
        STATUS_NOT_RECOMMENDED: "#991b1b",
    }.get(report.overall_status, "#475569")

    if report.flags:
        flag_rows = "".join(
            f"<tr><td>{_esc(flag.severity)}</td><td>{_esc(flag.code)}</td>"
            f"<td>{_esc(flag.employee_name)}</td><td>{_esc(flag.message)}</td></tr>"
            for flag in report.flags
        )
    else:
        flag_rows = (
            "<tr><td colspan='4' style='text-align:center;color:#166534;'>"
            "No fairness flags — schedule is within configured burnout thresholds.</td></tr>"
        )

    employee_rows = "".join(
        f"<tr><td>{_esc(row.employee_name)}</td><td>{_esc(row.peer_pool)}</td>"
        f"<td>{row.total_d}</td><td>{row.total_e}</td><td>{row.total_n}</td>"
        f"<td>{row.alternate_shifts}</td><td>{row.active_weekends}</td>"
        f"<td>{row.max_consecutive_work_days}</td><td>{row.max_consecutive_nights}</td>"
        f"<td>{len(row.flags)}</td></tr>"
        for row in report.employee_rows
    )

    attestation_block = ""
    if report.attestation_required:
        attestation_block = """
  <div class="attestation">
    <strong>Manager attestation required before breakroom posting</strong>
    <p>This schedule has fairness findings. A manager must review flags and record attestation
       in the scheduling workspace before export.</p>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>Staff Fairness Report — {_esc(report.period_name)}</title>
  <style>
    @page {{ margin: 18mm; }}
    body {{ font-family: Georgia, 'Times New Roman', serif; color: #1e293b; margin: 0; padding: 24px; }}
    h1 {{ font-size: 22px; margin: 0 0 4px; }}
    h2 {{ font-size: 15px; margin-top: 28px; border-bottom: 1px solid #cbd5e1; padding-bottom: 6px; }}
    .meta {{ font-size: 12px; color: #475569; margin-bottom: 20px; }}
    .badge {{ display: inline-block; padding: 8px 14px; border-radius: 6px; font-weight: 700;
              font-size: 12px; letter-spacing: 0.04em; background: {status_color}; color: #fff; }}
    .metrics {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }}
    .metric {{ border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; background: #f8fafc; }}
    .metric-label {{ font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #64748b; }}
    .metric-value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 10px; }}
    th {{ background: #0f172a; color: #fff; text-align: left; padding: 8px; }}
    td {{ border: 1px solid #e2e8f0; padding: 8px; vertical-align: top; }}
    .attestation {{ background: #fff7ed; border-left: 4px solid #92400e; padding: 14px; margin-top: 24px; font-size: 13px; }}
    footer {{ margin-top: 32px; font-size: 10px; color: #64748b; border-top: 1px solid #e2e8f0; padding-top: 12px; }}
  </style>
</head>
<body>
  <h1>Staff Fairness &amp; Burnout Review</h1>
  <div class="meta">
    {_esc(report.tenant_name)} · {_esc(report.period_name)}<br/>
    Period: {_esc(report.period_start.isoformat())} to {_esc(report.period_end.isoformat())}<br/>
    Generated (UTC): {_esc(report.generated_at_utc)} · Report ID: {_esc(report.report_id)}<br/>
    Integrity SHA-256: <code>{_esc(report.content_hash)}</code>
  </div>
  <p><span class="badge">{_esc(report.overall_status)}</span></p>
  <div class="metrics">
    <div class="metric"><div class="metric-label">Red flags</div><div class="metric-value">{report.red_flag_count}</div></div>
    <div class="metric"><div class="metric-label">Yellow flags</div><div class="metric-value">{report.yellow_flag_count}</div></div>
    <div class="metric"><div class="metric-label">Staff reviewed</div><div class="metric-value">{len(report.employee_rows)}</div></div>
    <div class="metric"><div class="metric-label">Peer pools</div><div class="metric-value">{len(report.pool_summaries)}</div></div>
  </div>
  <h2>Fairness flags</h2>
  <table>
    <thead><tr><th>Severity</th><th>Code</th><th>Employee</th><th>Detail</th></tr></thead>
    <tbody>{flag_rows}</tbody>
  </table>
  <h2>Per-employee workload summary</h2>
  <table>
    <thead>
      <tr>
        <th>Employee</th><th>Pool</th><th>D</th><th>E</th><th>N</th>
        <th>Alt</th><th>Active wknds</th><th>Max work streak</th><th>Max night streak</th><th>Flags</th>
      </tr>
    </thead>
    <tbody>{employee_rows}</tbody>
  </table>
  {attestation_block}
  <footer>
    Manager-only export — not for breakroom posting. Supports burnout prevention review before deployment.
  </footer>
</body>
</html>"""


def generate_staff_fairness_report(
    *,
    tenant_name: str,
    period_name: str,
    period_start: date,
    period_end: date,
    employees: Sequence[Mapping[str, object]],
    assignments: Sequence[Mapping[str, object]],
    shift_templates: Mapping[str, Mapping[str, object]],
    target_hours: Mapping[str, float],
    qual_lookup: Optional[Mapping[str, str]] = None,
    compliance_report: Optional[ComplianceReport] = None,
    thresholds: FairnessThresholds = DEFAULT_FAIRNESS_THRESHOLDS,
) -> Tuple[StaffFairnessReport, str]:
    report = build_staff_fairness_report(
        tenant_name=tenant_name,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        employees=employees,
        assignments=assignments,
        shift_templates=shift_templates,
        target_hours=target_hours,
        qual_lookup=qual_lookup,
        compliance_report=compliance_report,
        thresholds=thresholds,
    )
    return report, render_staff_fairness_report_html(report)
