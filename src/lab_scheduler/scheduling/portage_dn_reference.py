"""Screenshot-derived 8-week D/N master reference grids (MLT + MLA L01–04)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple

ReferenceEntry = Dict[str, object]
ShiftToken = str
WeekPattern = Tuple[ShiftToken, ShiftToken, ShiftToken, ShiftToken, ShiftToken, ShiftToken, ShiftToken]
CyclePattern = Tuple[WeekPattern, ...]

PORTAGE_CYCLE_WEEKS = 8

ReferenceEntry = Dict[str, object]

_FIXTURE_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "portage_dn_master_reference.json"
)

# Canonical D/N anchor constants (single source: portage_equity_targets).
from lab_scheduler.scheduling.portage_equity_targets import (
    PORTAGE_DN_FT_ALT_DENSITY,
    PORTAGE_DN_FT_NIGHT_SHIFT_TARGET,
    PORTAGE_DN_FT_PERIOD_WORK_SHIFTS,
    PORTAGE_DN_FT_WEEKEND_PAIRS,
)


def _blank_token(token: str) -> str:
    return "" if token in ("-", ".", " ") else token


def _entry_weeks(entry: ReferenceEntry) -> CyclePattern:
    raw_weeks = entry["weeks"]
    weeks: List[WeekPattern] = []
    for week in raw_weeks:
        tokens = tuple(_blank_token(str(token)) for token in week)
        if len(tokens) != 7:
            raise ValueError(f"Reference week requires 7 tokens, got {len(tokens)}: {week}")
        weeks.append(tokens)  # type: ignore[arg-type]
    if len(weeks) != PORTAGE_CYCLE_WEEKS:
        raise ValueError(f"Reference entry requires {PORTAGE_CYCLE_WEEKS} weeks, got {len(weeks)}")
    return tuple(weeks)


@lru_cache(maxsize=1)
def load_portage_dn_master_reference() -> Tuple[ReferenceEntry, ...]:
    payload = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    entries = tuple(payload["entries"])
    for entry in entries:
        _validate_reference_entry(entry)
    return entries



def find_day_night_adjacency_violations(weeks: CyclePattern) -> List[Tuple[int, int, str]]:
    """
    Return catalog tokens where Day is immediately followed by Night.

    Each item is `(week_index, day_index, kind)` where `kind` is
    `"in-week"` or `"cross-week"`.
    """

    violations: List[Tuple[int, int, str]] = []
    for week_index, week in enumerate(weeks):
        for day_index in range(6):
            if week[day_index] == "D" and week[day_index + 1] == "N":
                violations.append((week_index, day_index, "in-week"))
        if week_index + 1 < len(weeks) and week[6] == "D" and weeks[week_index + 1][0] == "N":
            violations.append((week_index, 6, "cross-week"))
    return violations


def validate_no_day_night_adjacency(weeks: CyclePattern) -> None:
    """Raise when any in-week or cross-week D→N pair exists in a catalog cycle."""

    violations = find_day_night_adjacency_violations(weeks)
    if violations:
        first = violations[0]
        raise ValueError(
            f"D→N adjacency forbidden at week {first[0]} day {first[1]} ({first[2]}); "
            f"{len(violations)} violation(s) in cycle"
        )


def sanitize_day_night_adjacency_in_cycle(weeks: CyclePattern) -> CyclePattern:
    """Drop the Day token before each illegal D→N handoff."""

    mutable = [list(week) for week in weeks]
    for week_index, week in enumerate(mutable):
        for day_index in range(6):
            if week[day_index] == "D" and week[day_index + 1] == "N":
                week[day_index] = ""
        if week_index + 1 < len(mutable) and week[6] == "D" and mutable[week_index + 1][0] == "N":
            week[6] = ""
    return tuple(tuple(week) for week in mutable)  # type: ignore[return-value]


WeekRow = List[ShiftToken]


def dn_weekend_catalog_week_indices(line: int) -> Tuple[int, int]:
    """
    1-based line number → the two 0-based catalog weeks with Sat/Sun night pairs.

    Line 01 → weeks 1–2 (indices 0–1); line 02 → weeks 3–4; etc.
    """

    if line < 1 or line > 4:
        raise ValueError(f"D/N weekend stagger supports lines 1–4, got {line}")
    first = 2 * (line - 1)
    return first, first + 1


def _dn_weekend_night_row(*, friday_night: bool = False) -> WeekRow:
    """
    Night block week: Mon–Thu nights, optional Friday night, Sat–Sun weekend nights.

    Standard (``friday_night=False``): 4 + Fri off + 2 weekend = 6N.
    Extended (``friday_night=True``): 5 + 2 weekend = 7N (Friday works once per 2-week block).
    """

    friday_token = "N" if friday_night else ""
    return ["N", "N", "N", "N", friday_token, "N", "N"]


def _placing_day_creates_dn_adjacency(
    weeks: Sequence[WeekRow],
    week_index: int,
    day_index: int,
    token: ShiftToken,
) -> bool:
    if token != "D":
        return False
    week = weeks[week_index]
    if day_index < 6 and week[day_index + 1] == "N":
        return True
    if day_index > 0 and week[day_index - 1] == "N":
        return False
    if day_index == 0 and week_index > 0 and weeks[week_index - 1][6] == "N":
        return False
    if day_index == 6 and week_index + 1 < len(weeks) and weeks[week_index + 1][0] == "N":
        return True
    return False


def _catalog_work_shift_count(weeks: Sequence[WeekRow]) -> int:
    return sum(1 for week in weeks for token in week if token)


def _trim_excess_catalog_day_tokens(
    weeks: List[WeekRow],
    *,
    target_work_shifts: int,
) -> None:
    """Drop day-band tokens from the tail until the 8-week grid matches 320h FT."""

    while _catalog_work_shift_count(weeks) > target_work_shifts:
        removed = False
        for week_index in range(PORTAGE_CYCLE_WEEKS - 1, -1, -1):
            for day_index in range(4, -1, -1):
                if weeks[week_index][day_index] != "D":
                    continue
                weeks[week_index][day_index] = ""
                removed = True
                break
            if removed:
                break
        if not removed:
            raise ValueError(
                f"unable to trim catalog to {target_work_shifts} work shifts "
                f"(stuck at {_catalog_work_shift_count(weeks)})"
            )


def staggered_dn_ft_cycle_for_line(line: int) -> CyclePattern:
    """
    Eight-week D/N full-time master with exclusive 2-week night blocks per line.

    Each line works exactly two catalog weeks of nights (Fri included) plus day
    weeks elsewhere. MLT and MLA share the same weeks by line number so the pool
    schedules exactly one night shift per qualification on every calendar day.
    """

    weekend_a, weekend_b = dn_weekend_catalog_week_indices(line)
    weekend_weeks = {weekend_a, weekend_b}
    weeks: List[WeekRow] = [["", "", "", "", "", "", ""] for _ in range(PORTAGE_CYCLE_WEEKS)]

    for week_index in sorted(weekend_weeks):
        weeks[week_index] = _dn_weekend_night_row(friday_night=True)

    for week_index, week in enumerate(weeks):
        if week_index in weekend_weeks:
            continue
        for day_index in range(5):
            if week[day_index]:
                continue
            if _placing_day_creates_dn_adjacency(weeks, week_index, day_index, "D"):
                continue
            week[day_index] = "D"

    _trim_excess_catalog_day_tokens(
        weeks,
        target_work_shifts=PORTAGE_DN_FT_PERIOD_WORK_SHIFTS,
    )

    cycle = tuple(tuple(week) for week in weeks)
    validate_no_day_night_adjacency(cycle)
    work_shifts = _catalog_work_shift_count(weeks)
    if work_shifts != PORTAGE_DN_FT_PERIOD_WORK_SHIFTS:
        raise ValueError(
            f"staggered D/N line {line}: expected {PORTAGE_DN_FT_PERIOD_WORK_SHIFTS} work shifts, "
            f"got {work_shifts}"
        )
    night_count = sum(1 for week in cycle for token in week if token == "N")
    if night_count != PORTAGE_DN_FT_NIGHT_SHIFT_TARGET:
        raise ValueError(
            f"staggered D/N line {line}: expected {PORTAGE_DN_FT_NIGHT_SHIFT_TARGET} N, got {night_count}"
        )
    weekend_pairs = sum(1 for week in cycle if week[5] == "N" and week[6] == "N")
    if weekend_pairs != PORTAGE_DN_FT_WEEKEND_PAIRS:
        raise ValueError(
            f"staggered D/N line {line}: expected {PORTAGE_DN_FT_WEEKEND_PAIRS} weekend pairs, "
            f"got {weekend_pairs}"
        )
    return cycle


def validate_pool_exactly_one_night_per_day(*, role: str) -> None:
    """Raise when the D/N pool would schedule more or less than one night per day."""

    counts = pool_daily_night_counts(role)
    violations = [(index, count) for index, count in enumerate(counts) if count != 1]
    if violations:
        first_index, first_count = violations[0]
        raise ValueError(
            f"{role} D/N pool: day index {first_index} has {first_count} night(s); "
            f"expected exactly 1 across {len(violations)} day(s)"
        )


def _validate_reference_entry(entry: ReferenceEntry) -> None:
    weeks = _entry_weeks(entry)
    validate_no_day_night_adjacency(weeks)
    night_count = sum(1 for week in weeks for token in week if token == "N")
    if night_count != PORTAGE_DN_FT_NIGHT_SHIFT_TARGET:
        raise ValueError(
            f"{entry['role']} line {entry['line']}: expected "
            f"{PORTAGE_DN_FT_NIGHT_SHIFT_TARGET} N tokens, got {night_count}"
        )
    for week_index, week in enumerate(weeks):
        if week[5] == "D" or week[6] == "D":
            raise ValueError(
                f"{entry['role']} line {entry['line']} week {week_index}: D on weekend forbidden"
            )
    weekend_pairs = sum(1 for week in weeks if week[5] == "N" and week[6] == "N")
    if weekend_pairs != PORTAGE_DN_FT_WEEKEND_PAIRS:
        raise ValueError(
            f"{entry['role']} line {entry['line']}: expected "
            f"{PORTAGE_DN_FT_WEEKEND_PAIRS} weekend N pairs, got {weekend_pairs}"
        )
    work_shifts = sum(1 for week in weeks for token in week if token)
    if work_shifts != PORTAGE_DN_FT_PERIOD_WORK_SHIFTS:
        raise ValueError(
            f"{entry['role']} line {entry['line']}: expected "
            f"{PORTAGE_DN_FT_PERIOD_WORK_SHIFTS} catalog work shifts (320h), got {work_shifts}"
        )


def reference_entry(*, role: str, line: int) -> ReferenceEntry:
    role = role.upper()
    for entry in load_portage_dn_master_reference():
        if entry["role"] == role and int(entry["line"]) == line:
            return entry
    raise KeyError(f"No D/N reference entry for {role} line {line}")


def reference_cycle_for_line(*, role: str, line: int) -> CyclePattern:
    return _entry_weeks(reference_entry(role=role, line=line))


def reference_baseline_cycle(role: str) -> CyclePattern:
    """Line 01 offset-0 baseline pattern for ``role``."""

    return reference_cycle_for_line(role=role, line=1)


def pool_daily_night_counts(
    role: str,
    *,
    lines: Sequence[int] = (1, 2, 3, 4),
) -> List[int]:
    """Sum of ``N`` tokens across ``lines`` for each day in the 8-week cycle."""

    counts = [0] * (PORTAGE_CYCLE_WEEKS * 7)
    for line in lines:
        weeks = reference_cycle_for_line(role=role, line=line)
        for week_index, week in enumerate(weeks):
            for day_index, token in enumerate(week):
                if token == "N":
                    counts[week_index * 7 + day_index] += 1
    return counts


def validate_pool_coverage(*, role: str, minimum_nights: int = 1) -> None:
    validate_pool_exactly_one_night_per_day(role=role)
