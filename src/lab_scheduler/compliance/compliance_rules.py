from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MANITOBA_MIN_REST_BEFORE_MORNING_HOURS = 11.0
UNION_MIN_TURNAROUND_HOURS = 15.0
CLINICAL_FLOOR_MAX_STRETCH_HOURS = 24.0
PORTAGE_WARNING_CONSECUTIVE_DAYS = 6
from lab_scheduler.errors.schedule_error import (
    APPROVED_STRETCH_CODE,
    CONSECUTIVE_DAYS_WARNING_CODE,
    JOANNE_STYLE_STRETCH_CODE,
)


@dataclass(frozen=True, slots=True)
class ShiftTransition:
    code: str
    start: datetime
    end: datetime


def normalize_shift_code(code: str) -> str:
    text = str(code).strip().upper()
    if text.startswith("MOR") or text == "M":
        return "M"
    if text.startswith("EVE") or text == "E":
        return "E"
    if text.startswith("NIG") or text == "N":
        return "N"
    return text[:1] if text else ""


def check_11_hour_rest(
    shift_a: ShiftTransition,
    shift_b: ShiftTransition,
    *,
    minimum_hours: float = MANITOBA_MIN_REST_BEFORE_MORNING_HOURS,
) -> bool:
    """
    Return True when the transition satisfies Manitoba morning-rest rules.

    Returns False when shift_a is Evening or Night and shift_b is Morning
    with less than 11 hours between the end of shift_a and start of shift_b.
    """

    prior_code = normalize_shift_code(shift_a.code)
    next_code = normalize_shift_code(shift_b.code)
    if next_code != "M":
        return True
    if prior_code not in {"E", "N"}:
        return True
    gap_hours = (shift_b.start - shift_a.end).total_seconds() / 3600.0
    return gap_hours >= minimum_hours - 1e-9


def check_11_hour_rest_chain(transitions: list[ShiftTransition]) -> bool:
    """Return True when every consecutive transition satisfies check_11_hour_rest."""

    if len(transitions) < 2:
        return True
    ordered = sorted(transitions, key=lambda shift: shift.start)
    for index in range(1, len(ordered)):
        if not check_11_hour_rest(ordered[index - 1], ordered[index]):
            return False
    return True


def turnaround_gap_hours(
    shift_a: ShiftTransition,
    shift_b: ShiftTransition,
) -> float:
    return (shift_b.start - shift_a.end).total_seconds() / 3600.0


def stretch_span_hours(
    shift_a: ShiftTransition,
    shift_b: ShiftTransition,
) -> float:
    """Wall-clock span from the start of the earlier shift through the end of the later shift."""

    return (shift_b.end - shift_a.start).total_seconds() / 3600.0


def clinical_floor_stretch_allowed(
    shift_a: ShiftTransition,
    shift_b: ShiftTransition,
) -> bool:
    """
    True when an Evening/Night-to-Morning transition is below the 15h union turnaround
    but the combined stretch remains within the 24h clinical-floor ceiling.
    """

    gap = turnaround_gap_hours(shift_a, shift_b)
    if gap >= UNION_MIN_TURNAROUND_HOURS - 1e-9:
        return False
    prior_code = normalize_shift_code(shift_a.code)
    next_code = normalize_shift_code(shift_b.code)
    if prior_code not in {"E", "N"} or next_code != "M":
        return False
    return stretch_span_hours(shift_a, shift_b) <= CLINICAL_FLOOR_MAX_STRETCH_HOURS + 1e-9
