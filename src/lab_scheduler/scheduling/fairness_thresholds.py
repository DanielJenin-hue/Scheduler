"""Shared staff-fairness thresholds for CP-SAT objectives and manager reports."""

from __future__ import annotations

from dataclasses import dataclass

from lab_scheduler.scheduling.portage_equity_targets import (
    portage_active_weekend_target,
    PORTAGE_FULLTIME_PERIOD_HOURS,
)

# CP-SAT soft objective weights (Tier B layout preferences; must stay below contract tier).
WEIGHT_EVENING_CLUSTER = 350
WEIGHT_POST_NIGHT_RECOVERY = 350
DEFAULT_FAIRNESS_WEIGHT_SCALE = 1.0
FAIRNESS_RERUN_WEIGHT_SCALE = 2.0
FULLTIME_ACTIVE_WEEKENDS_REQUIRED = portage_active_weekend_target(
    PORTAGE_FULLTIME_PERIOD_HOURS
)

# Production CP-SAT time budgets (seconds per pass).
CPSAT_PRIMARY_TIME_LIMIT_SECONDS = 60.0
CPSAT_INTERACTIVE_PORTAGE_PRIMARY_TIME_LIMIT_SECONDS = 75.0
CPSAT_FAIRNESS_RERUN_TIME_LIMIT_SECONDS = 45.0
CPSAT_GAP_CLOSURE_TIME_LIMIT_SECONDS = 30.0

# Fairness flag codes that pass-2 CP-SAT can meaningfully improve.
SOLVER_ADDRESSABLE_FAIRNESS_CODES = frozenset(
    {"EVENING_CLUSTER", "POST_NIGHT_RECOVERY"}
)


@dataclass(frozen=True, slots=True)
class FairnessThresholds:
    alt_shift_variance_shifts: int = 1
    evening_cluster_window_days: int = 14
    evening_cluster_max: int = 3
    post_night_recovery_off_days: int = 2
    contract_hours_tolerance: float = 8.0
    weekend_excess_above_floor: int = 1
    fulltime_active_weekends_required: int = FULLTIME_ACTIVE_WEEKENDS_REQUIRED


DEFAULT_FAIRNESS_THRESHOLDS = FairnessThresholds()
