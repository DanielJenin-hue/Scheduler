from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Mapping

from lab_scheduler.compliance.compliance_rules import (
    MANITOBA_MIN_REST_BEFORE_MORNING_HOURS,
    UNION_MIN_TURNAROUND_HOURS,
)
from lab_scheduler.scheduling.contract_payroll import HOURS_PER_SHIFT
from lab_scheduler.scheduling.schedule_tallies import (
    WEEKDAY_SHIFT_TARGETS,
    WEEKEND_SHIFT_TARGETS,
)


@dataclass(frozen=True, slots=True)
class UnionRulesPortage:
    """Portage CBA policy constants for the master schedule grid."""

    hours_per_shift: float = HOURS_PER_SHIFT
    union_min_turnaround_hours: float = UNION_MIN_TURNAROUND_HOURS
    manitoba_min_rest_before_morning_hours: float = MANITOBA_MIN_REST_BEFORE_MORNING_HOURS
    biweekly_normal_hours: float = 80.0
    weekday_shift_targets: Mapping[str, int] = field(default_factory=lambda: dict(WEEKDAY_SHIFT_TARGETS))
    weekend_shift_targets: Mapping[str, int] = field(default_factory=lambda: dict(WEEKEND_SHIFT_TARGETS))
    allow_autonomous_contract_line_changes: bool = False


UNION_RULES_PORTAGE = UnionRulesPortage()


def is_portage_weekend(day: date) -> bool:
    """Calendar weekend days (Saturday/Sunday) for seat targets."""

    return day.weekday() >= 5


def _weekend_block_start(day: date) -> date:
    """Saturday of the weekend block containing ``day``."""

    weekday = day.weekday()
    if weekday == 5:
        return day
    if weekday == 6:
        return day - timedelta(days=1)
    days_until_saturday = (5 - weekday) % 7
    return day + timedelta(days=days_until_saturday)


def portage_weekend_window_start(day: date) -> datetime:
    """Saturday 00:01 for the weekend block containing ``day``."""

    saturday = _weekend_block_start(day)
    return datetime.combine(saturday, time(0, 1))


def portage_weekend_window_end(day: date) -> datetime:
    """Sunday 24:00 (Monday 00:00) for the weekend block containing ``day``."""

    saturday = _weekend_block_start(day)
    sunday = saturday + timedelta(days=1)
    return datetime.combine(sunday + timedelta(days=1), time(0, 0))


def is_in_weekend_rest_window(moment: datetime) -> bool:
    """True when ``moment`` falls in the 48h Sat 00:01 – Sun 24:00 block."""

    window_start = portage_weekend_window_start(moment.date())
    window_end = portage_weekend_window_end(moment.date())
    return window_start <= moment < window_end


def shift_target_for_portage_date(day: date, band: str) -> int:
    """Operational D/E/N seat target for a calendar day."""

    token = band.strip().upper()
    if token == "MORNING":
        token = "D"
    targets = (
        UNION_RULES_PORTAGE.weekend_shift_targets
        if is_portage_weekend(day)
        else UNION_RULES_PORTAGE.weekday_shift_targets
    )
    return int(targets.get(token, 0))
