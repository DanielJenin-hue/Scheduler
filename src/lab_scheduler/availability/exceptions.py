from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.jurisdictions import JurisdictionRules

OFF_CODE_VACATION = "V"
OFF_CODE_SICK = "I"
AVAILABILITY_OFF_CODES: frozenset[str] = frozenset({OFF_CODE_VACATION, OFF_CODE_SICK})


@dataclass(frozen=True, slots=True)
class AvailabilityException:
    id: str
    tenant_id: str
    employee_id: str
    start_date: date
    end_date: date
    reason: str
    status: str = "approved"


def reason_to_off_code(reason: str) -> str:
    lower = reason.lower()
    if "sick" in lower:
        return OFF_CODE_SICK
    return OFF_CODE_VACATION


def off_code_label(code: str) -> str:
    if code == OFF_CODE_SICK:
        return "Off (Sick)"
    if code == OFF_CODE_VACATION:
        return "Off (Vacation)"
    return "Off"


def is_availability_off_code(code: str) -> bool:
    return code in AVAILABILITY_OFF_CODES


def _daterange(start: date, end_inclusive: date) -> Iterable[date]:
    cur = start
    while cur <= end_inclusive:
        yield cur
        cur += timedelta(days=1)


def expand_blocked_dates(
    exceptions: Sequence[AvailabilityException],
    *,
    period_start: Optional[date] = None,
    period_end: Optional[date] = None,
    approved_only: bool = True,
) -> Dict[str, Dict[date, str]]:
    """Map employee_id -> {date: reason} for each blocked calendar day."""

    blocked: Dict[str, Dict[date, str]] = {}
    for exc in exceptions:
        if approved_only and exc.status.lower() != "approved":
            continue
        for d in _daterange(exc.start_date, exc.end_date):
            if period_start is not None and d < period_start:
                continue
            if period_end is not None and d > period_end:
                continue
            blocked.setdefault(exc.employee_id, {})[d] = exc.reason
    return blocked


def blocked_dates_by_employee(
    blocked_map: Mapping[str, Mapping[date, str]],
) -> Dict[str, Set[date]]:
    return {emp_id: set(days.keys()) for emp_id, days in blocked_map.items()}


def is_date_blocked(
    employee_id: str,
    on_date: date,
    blocked_map: Mapping[str, Mapping[date, str]],
) -> Optional[str]:
    return blocked_map.get(employee_id, {}).get(on_date)


def count_blocked_days_in_period(
    blocked_dates: Set[date],
    *,
    period_start: date,
    period_end: date,
) -> int:
    return sum(1 for d in blocked_dates if period_start <= d <= period_end)


def adjusted_target_hours(
    *,
    rules: JurisdictionRules,
    fte: float,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    blocked_dates: Set[date],
) -> float:
    """
    Reduce contracted period hours proportionally for approved time off.

    Example: 1.0 FTE at 40h/week over 4 weeks = 160h base.
    One full week off (7 of 28 days) -> 120h adjusted target.
    """

    full_target = rules.standard_hours_per_week_at_1_0_fte * fte * weeks_in_period
    total_days = (period_end - period_start).days + 1
    if total_days <= 0 or not blocked_dates:
        return round(full_target, 2)

    blocked_in_period = count_blocked_days_in_period(
        blocked_dates, period_start=period_start, period_end=period_end
    )
    if blocked_in_period <= 0:
        return round(full_target, 2)

    ratio_available = 1.0 - (blocked_in_period / total_days)
    return round(full_target * ratio_available, 2)


EMERGENCY_SICK_LEAVE_REASON = "Sick Leave"


def create_availability_exception(
    conn,
    *,
    tenant_id: str,
    employee_id: str,
    start_date: date,
    end_date: date,
    reason: str = EMERGENCY_SICK_LEAVE_REASON,
    status: str = "approved",
    exception_id: Optional[str] = None,
) -> str:
    """Insert an approved availability exception and return its id."""

    import uuid
    from datetime import datetime, timezone

    exc_id = exception_id or f"avail-{uuid.uuid4().hex[:12]}"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn.execute(
        """
        INSERT INTO availability_exceptions (
          id, tenant_id, employee_id, start_date, end_date, reason, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            exc_id,
            tenant_id,
            employee_id,
            start_date.isoformat(),
            end_date.isoformat(),
            reason,
            status,
            now,
            now,
        ),
    )
    conn.commit()
    return exc_id


def compute_employee_target_hours(
    *,
    rules: JurisdictionRules,
    employees: Sequence[Mapping[str, object]],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    blocked_map: Mapping[str, Mapping[date, str]],
) -> Dict[str, float]:
    by_emp = blocked_dates_by_employee(blocked_map)
    out: Dict[str, float] = {}
    for emp in employees:
        emp_id = str(emp["id"])
        fte = float(emp["fte"])
        out[emp_id] = adjusted_target_hours(
            rules=rules,
            fte=fte,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            blocked_dates=by_emp.get(emp_id, set()),
        )
    return out
