"""Declarative Portage alternate-shift rotation rules."""

from __future__ import annotations

from dataclasses import dataclass

from lab_scheduler.scheduling.schedule_tallies import (
    WEEKDAY_SHIFT_TARGETS,
    WEEKEND_SHIFT_TARGETS,
)

# FT D/E: one calendar week of straight E per line (Mon–Sun), staggered across qual pool.
FT_DE_EVENING_BLOCK_DAYS = 7
FT_DE_EVENING_TARGET = 8
FT_DE_EVENING_BLOCK_STREAK_DAYS = 7  # exception to default 6-day Portage cap

# Operational weekend stagger (4 shift-days); catalog may label 8 aspirational.
FT_WEEKEND_SHIFT_DAYS_OPERATIONAL = 4

# Portage gold standard: same employee, same band on Sat and Sun (or neither day).
WEEKEND_MIRROR_BANDS = frozenset({"D", "E", "N"})


@dataclass(frozen=True, slots=True)
class EveningBlockSpec:
    block_days: int = FT_DE_EVENING_BLOCK_DAYS
    consecutive: bool = True
    stagger_weeks: int = 1
    streak_exception_days: int = FT_DE_EVENING_BLOCK_STREAK_DAYS
    ft_evening_target: int = FT_DE_EVENING_TARGET


@dataclass(frozen=True, slots=True)
class StaffingProfile:
    weekday_targets: dict[str, int]
    weekend_targets: dict[str, int]

    @classmethod
    def portage_default(cls) -> StaffingProfile:
        return cls(
            weekday_targets=dict(WEEKDAY_SHIFT_TARGETS),
            weekend_targets=dict(WEEKEND_SHIFT_TARGETS),
        )


DEFAULT_EVENING_BLOCK = EveningBlockSpec()
DEFAULT_STAFFING = StaffingProfile.portage_default()
