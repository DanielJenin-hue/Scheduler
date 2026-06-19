from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Set, Tuple

from .profiles import EmployeeProfile


@dataclass(frozen=True, slots=True)
class SeniorityBypassInfo:
    """CBA grievance-prevention context when a non-senior candidate is selected."""

    selected_employee_id: str
    selected_employee_name: str
    most_senior_qualified_id: str
    most_senior_qualified_name: str
    most_senior_eligible: bool
    justification: str
    requires_manual_justification: bool


def cba_rank_key(profile: EmployeeProfile) -> Tuple[float, float, float]:
    """
    Collective bargaining rank (best candidate sorts first):
      1. Seniority hours (descending)
      2. Part-time / lower FTE (ascending — reduces OT premium exposure)
      3. Hourly wage (ascending — lowest cost)
    """

    return (-profile.seniority_hours, profile.fte, profile.base_hourly_rate)


def rank_profiles_cba(profiles: Sequence[EmployeeProfile]) -> list[EmployeeProfile]:
    return sorted(profiles, key=cba_rank_key)


def most_senior_qualified(profiles: Sequence[EmployeeProfile]) -> Optional[EmployeeProfile]:
    if not profiles:
        return None
    return max(
        profiles,
        key=lambda profile: (
            profile.seniority_hours,
            -profile.fte,
            -profile.base_hourly_rate,
        ),
    )


def evaluate_seniority_bypass(
    *,
    qualified_profiles: Sequence[EmployeeProfile],
    eligible_ids: Set[str],
    selected: EmployeeProfile,
    ineligible_reasons: Optional[Mapping[str, str]] = None,
) -> Optional[SeniorityBypassInfo]:
    """
    Flag when the selected employee is not the most senior role-qualified person.

    Manual justification is mandatory when the most senior qualified employee was
    eligible but a junior employee was selected instead.
    """

    most_senior = most_senior_qualified(qualified_profiles)
    if most_senior is None or selected.id == most_senior.id:
        return None

    if (
        selected.seniority_hours,
        selected.fte,
        selected.base_hourly_rate,
    ) == (
        most_senior.seniority_hours,
        most_senior.fte,
        most_senior.base_hourly_rate,
    ):
        return None

    ineligible_reasons = ineligible_reasons or {}
    most_senior_eligible = most_senior.id in eligible_ids

    if most_senior_eligible:
        justification = (
            f"Selected {selected.full_name} over most senior qualified "
            f"{most_senior.full_name} ({most_senior.seniority_hours:.0f} seniority hours)."
        )
        requires_manual = True
    else:
        block_reason = ineligible_reasons.get(most_senior.id, "labor rule violation")
        justification = (
            f"Most senior qualified {most_senior.full_name} "
            f"({most_senior.seniority_hours:.0f} seniority hours) unavailable: {block_reason}."
        )
        requires_manual = False

    return SeniorityBypassInfo(
        selected_employee_id=selected.id,
        selected_employee_name=selected.full_name,
        most_senior_qualified_id=most_senior.id,
        most_senior_qualified_name=most_senior.full_name,
        most_senior_eligible=most_senior_eligible,
        justification=justification,
        requires_manual_justification=requires_manual,
    )
