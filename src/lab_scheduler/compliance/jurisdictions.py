from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass(frozen=True, slots=True)
class JurisdictionRules:
    """
    Provincial employment-standards parameters for scheduling compliance.

    Sources (general industry rules; exemptions/averaging agreements not modeled):
    - Manitoba: Employment Standards Code / overtime & hours factsheets
    - Ontario: Employment Standards Act, 2000 (ESA) — overtime & hours of work
    """

    code: str
    display_name: str
    # Overtime
    daily_overtime_threshold_hours: Optional[float]  # None = no daily OT trigger
    weekly_overtime_threshold_hours: float
    overtime_rate_multiplier: float
    # Rest
    min_rest_between_shifts_hours: Optional[float]
    min_daily_rest_hours: Optional[float]
    min_weekly_rest_hours: float
    max_consecutive_work_days: int  # bi-weekly scheduling ceiling (ESA/MB practice)
    max_work_days_per_work_week: int  # Monday-start week: need 24h rest → max 6 work days
    # Breaks
    break_after_consecutive_hours: Optional[float]
    break_minutes: Optional[int]
    # Scheduling caps (Ontario ESA s.17 — general limit before written agreement)
    max_scheduled_hours_per_day: Optional[float]
    # FTE labor forecasting baseline (matches weekly standard hours)
    standard_hours_per_week_at_1_0_fte: float
    citation_label: str
    citation_urls: Tuple[str, ...]


MANITOBA = JurisdictionRules(
    code="MB",
    display_name="Manitoba",
    daily_overtime_threshold_hours=8.0,
    weekly_overtime_threshold_hours=40.0,
    overtime_rate_multiplier=1.5,
    min_rest_between_shifts_hours=None,
    min_daily_rest_hours=None,
    min_weekly_rest_hours=24.0,
    max_consecutive_work_days=12,
    max_work_days_per_work_week=6,
    break_after_consecutive_hours=5.0,
    break_minutes=30,
    max_scheduled_hours_per_day=None,
    standard_hours_per_week_at_1_0_fte=40.0,
    citation_label="Manitoba Employment Standards (8h/day, 40h/week OT; 24h weekly rest)",
    citation_urls=(
        "https://www.gov.mb.ca/labour/standards/doc,overtime,factsheet.html",
        "https://www.gov.mb.ca/labour/standards/doc,hours-and-breaks,factsheet.html",
    ),
)

ONTARIO = JurisdictionRules(
    code="ON",
    display_name="Ontario",
    daily_overtime_threshold_hours=None,  # no daily OT under general ESA rule
    weekly_overtime_threshold_hours=44.0,
    overtime_rate_multiplier=1.5,
    min_rest_between_shifts_hours=8.0,
    min_daily_rest_hours=11.0,
    min_weekly_rest_hours=24.0,
    max_consecutive_work_days=12,
    max_work_days_per_work_week=6,
    break_after_consecutive_hours=None,
    break_minutes=None,
    max_scheduled_hours_per_day=8.0,
    standard_hours_per_week_at_1_0_fte=44.0,
    citation_label="Ontario ESA, 2000 (44h/week OT; 11h daily rest; 8h between shifts)",
    citation_urls=(
        "https://www.ontario.ca/document/your-guide-employment-standards-act-0/overtime-pay",
        "https://www.ontario.ca/document/your-guide-employment-standards-act-0/hours-work",
    ),
)

JURISDICTIONS: Dict[str, JurisdictionRules] = {
    MANITOBA.display_name: MANITOBA,
    ONTARIO.display_name: ONTARIO,
}

DEFAULT_JURISDICTION_NAME = MANITOBA.display_name


def get_jurisdiction(name: str) -> JurisdictionRules:
    try:
        return JURISDICTIONS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown jurisdiction: {name!r}") from exc
