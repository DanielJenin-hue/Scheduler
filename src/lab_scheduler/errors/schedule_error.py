from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class ScheduleErrorCategory(StrEnum):
    """High-level grouping for schedule error codes."""

    LABOR = "labor"
    COVERAGE = "coverage"
    CLINICAL = "clinical"
    CONTRACT = "contract"
    OVERTIME = "overtime"
    STRETCH = "stretch"
    WARNING = "warning"
    VALIDATION = "validation"


@dataclass(frozen=True, slots=True)
class ScheduleErrorMeta:
    category: ScheduleErrorCategory
    manager_label: str
    severity: str = "error"
    legacy_aliases: frozenset[str] = frozenset()


class ScheduleError(StrEnum):
    """
    Unified registry for schedule generation, compliance, and coverage errors.

    Replaces scattered string literals and legacy module-level constants.
    """

    # Slot / generation violation kinds
    LABOR_RULE = "LABOR_RULE"
    COVERAGE_TARGET = "COVERAGE_TARGET"
    ERR_IMPOSSIBLE_COVERAGE = "ERR_IMPOSSIBLE_COVERAGE"
    ERR_NON_MONDAY_BLOCK_START = "ERR_NON_MONDAY_BLOCK_START"

    # Statutory / union labor (compliance engine + validator)
    DAILY_REST = "DAILY_REST"
    BETWEEN_SHIFTS = "BETWEEN_SHIFTS"
    OVERLAPPING_SHIFTS = "OVERLAPPING_SHIFTS"
    WEEKLY_REST = "WEEKLY_REST"
    CONSECUTIVE_DAYS = "CONSECUTIVE_DAYS"
    PORTAGE_CONSECUTIVE_DAYS = "PORTAGE_CONSECUTIVE_DAYS"
    MAX_DAILY_HOURS = "MAX_DAILY_HOURS"
    MAX_WEEKLY_HOURS = "MAX_WEEKLY_HOURS"
    WEEKLY_OVERTIME = "WEEKLY_OVERTIME"
    DAILY_OVERTIME = "DAILY_OVERTIME"
    UNPAID_BREAK = "UNPAID_BREAK"
    UNION_TURNAROUND_15H = "UNION_TURNAROUND_15H"
    UNION_MORNING_REST_11H = "UNION_MORNING_REST_11H"

    # Clinical floor
    CLINICAL_MORNING = "CLINICAL_MORNING"
    CLINICAL_EVENING = "CLINICAL_EVENING"
    CLINICAL_NIGHT = "CLINICAL_NIGHT"
    CRITICAL_CLINICAL_GAP = "CRITICAL_CLINICAL_GAP"
    CLINICAL_GAP_REMAINS = "CLINICAL_GAP_REMAINS"
    WEEKEND_CLINICAL_FLOOR = "WEEKEND_CLINICAL_FLOOR"
    WEEKEND_STAFFING_CAP = "WEEKEND_STAFFING_CAP"
    WEEKDAY_DAY_SHIFT_CAPACITY = "WEEKDAY_DAY_SHIFT_CAPACITY"

    # Contract / payroll
    CONTRACT_FTE_160 = "CONTRACT_FTE_160"

    # Overtime bypass (revoked in strict runs; retained for audit surfacing)
    OVERTIME_REQUIRED_COMPLIANCE_BYPASSED = "OVERTIME_REQUIRED_COMPLIANCE_BYPASSED"

    # Stretch / warning flags
    APPROVED_STRETCH = "APPROVED_STRETCH"
    JOANNE_STYLE_CLINICAL_STRETCH = "JOANNE_STYLE_CLINICAL_STRETCH"
    CONSECUTIVE_DAYS_WARNING = "CONSECUTIVE_DAYS_WARNING"

    @property
    def meta(self) -> ScheduleErrorMeta:
        return SCHEDULE_ERROR_REGISTRY[self]

    @property
    def category(self) -> ScheduleErrorCategory:
        return self.meta.category

    @property
    def manager_label(self) -> str:
        return self.meta.manager_label

    @property
    def severity(self) -> str:
        return self.meta.severity

    @classmethod
    def clinical_floor(cls, shift_code: str) -> ScheduleError:
        normalized = str(shift_code).strip().upper()
        mapping = {
            "MORNING": cls.CLINICAL_MORNING,
            "EVENING": cls.CLINICAL_EVENING,
            "NIGHT": cls.CLINICAL_NIGHT,
        }
        try:
            return mapping[normalized]
        except KeyError as exc:
            raise ValueError(f"Unknown clinical floor shift code: {shift_code!r}") from exc


SCHEDULE_ERROR_REGISTRY: Mapping[ScheduleError, ScheduleErrorMeta] = {
    ScheduleError.LABOR_RULE: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Labor rule violation",
    ),
    ScheduleError.COVERAGE_TARGET: ScheduleErrorMeta(
        category=ScheduleErrorCategory.COVERAGE,
        manager_label="Coverage target gap",
    ),
    ScheduleError.ERR_IMPOSSIBLE_COVERAGE: ScheduleErrorMeta(
        category=ScheduleErrorCategory.COVERAGE,
        manager_label="Impossible coverage — human intervention required",
        legacy_aliases=frozenset({"IMPOSSIBLE_COVERAGE"}),
    ),
    ScheduleError.ERR_NON_MONDAY_BLOCK_START: ScheduleErrorMeta(
        category=ScheduleErrorCategory.VALIDATION,
        manager_label="Schedule block must start on Monday",
    ),
    ScheduleError.DAILY_REST: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Daily rest violation",
    ),
    ScheduleError.BETWEEN_SHIFTS: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Rest period violation",
    ),
    ScheduleError.OVERLAPPING_SHIFTS: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Overlapping shift violation",
    ),
    ScheduleError.WEEKLY_REST: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Weekly rest violation",
    ),
    ScheduleError.CONSECUTIVE_DAYS: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Consecutive work-day violation",
    ),
    ScheduleError.PORTAGE_CONSECUTIVE_DAYS: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Portage consecutive work-day violation",
    ),
    ScheduleError.MAX_DAILY_HOURS: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Max daily hours violation",
    ),
    ScheduleError.MAX_WEEKLY_HOURS: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Max weekly hours violation",
    ),
    ScheduleError.WEEKLY_OVERTIME: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Max weekly hours violation",
    ),
    ScheduleError.DAILY_OVERTIME: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="Daily overtime violation",
    ),
    ScheduleError.UNPAID_BREAK: ScheduleErrorMeta(
        category=ScheduleErrorCategory.WARNING,
        manager_label="Unpaid break warning",
        severity="warning",
    ),
    ScheduleError.UNION_TURNAROUND_15H: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="15h turnaround violation",
    ),
    ScheduleError.UNION_MORNING_REST_11H: ScheduleErrorMeta(
        category=ScheduleErrorCategory.LABOR,
        manager_label="11h morning rest violation",
    ),
    ScheduleError.CLINICAL_MORNING: ScheduleErrorMeta(
        category=ScheduleErrorCategory.CLINICAL,
        manager_label="Clinical floor violation (Morning)",
    ),
    ScheduleError.CLINICAL_EVENING: ScheduleErrorMeta(
        category=ScheduleErrorCategory.CLINICAL,
        manager_label="Clinical floor violation (Evening)",
    ),
    ScheduleError.CLINICAL_NIGHT: ScheduleErrorMeta(
        category=ScheduleErrorCategory.CLINICAL,
        manager_label="Clinical floor violation (Night)",
    ),
    ScheduleError.CRITICAL_CLINICAL_GAP: ScheduleErrorMeta(
        category=ScheduleErrorCategory.CLINICAL,
        manager_label="Critical clinical gap",
    ),
    ScheduleError.CLINICAL_GAP_REMAINS: ScheduleErrorMeta(
        category=ScheduleErrorCategory.CLINICAL,
        manager_label="Clinical gap remains after fill attempt",
    ),
    ScheduleError.WEEKEND_CLINICAL_FLOOR: ScheduleErrorMeta(
        category=ScheduleErrorCategory.CLINICAL,
        manager_label="Weekend staffing floor (1 MLT + 1 MLA)",
    ),
    ScheduleError.WEEKEND_STAFFING_CAP: ScheduleErrorMeta(
        category=ScheduleErrorCategory.CLINICAL,
        manager_label="Weekend staffing cap (1 MLT + 1 MLA)",
    ),
    ScheduleError.WEEKDAY_DAY_SHIFT_CAPACITY: ScheduleErrorMeta(
        category=ScheduleErrorCategory.COVERAGE,
        manager_label="Weekday day-shift capacity limit (14 staff)",
    ),
    ScheduleError.CONTRACT_FTE_160: ScheduleErrorMeta(
        category=ScheduleErrorCategory.CONTRACT,
        manager_label="Contract FTE violation (period target)",
    ),
    ScheduleError.OVERTIME_REQUIRED_COMPLIANCE_BYPASSED: ScheduleErrorMeta(
        category=ScheduleErrorCategory.OVERTIME,
        manager_label="OVERTIME-REQUIRED-COMPLIANCE-BYPASSED",
    ),
    ScheduleError.APPROVED_STRETCH: ScheduleErrorMeta(
        category=ScheduleErrorCategory.STRETCH,
        manager_label="Manager approved stretch",
        severity="warning",
    ),
    ScheduleError.JOANNE_STYLE_CLINICAL_STRETCH: ScheduleErrorMeta(
        category=ScheduleErrorCategory.STRETCH,
        manager_label="Joanne-style clinical stretch",
        severity="warning",
    ),
    ScheduleError.CONSECUTIVE_DAYS_WARNING: ScheduleErrorMeta(
        category=ScheduleErrorCategory.WARNING,
        manager_label="Consecutive days warning",
        severity="warning",
    ),
}

# Legacy string constants — prefer ScheduleError members in new code.
VIOLATION_LABOR_RULE = ScheduleError.LABOR_RULE.value
VIOLATION_COVERAGE_TARGET = ScheduleError.COVERAGE_TARGET.value
VIOLATION_IMPOSSIBLE_COVERAGE = ScheduleError.ERR_IMPOSSIBLE_COVERAGE.value

APPROVED_STRETCH_CODE = ScheduleError.APPROVED_STRETCH.value
JOANNE_STYLE_STRETCH_CODE = ScheduleError.JOANNE_STYLE_CLINICAL_STRETCH.value
CONSECUTIVE_DAYS_WARNING_CODE = ScheduleError.CONSECUTIVE_DAYS_WARNING.value

CRITICAL_CLINICAL_GAP_CODE = ScheduleError.CRITICAL_CLINICAL_GAP.value
OVERTIME_COMPLIANCE_BYPASS_LABEL = ScheduleError.OVERTIME_REQUIRED_COMPLIANCE_BYPASSED.meta.manager_label

IMPOSSIBLE_COVERAGE_TOOLTIP = (
    "Insufficient staffing capacity to meet coverage target"
)

_CODE_LOOKUP: dict[str, ScheduleError] = {}
for member in ScheduleError:
    _CODE_LOOKUP[member.value] = member
    for alias in member.meta.legacy_aliases:
        _CODE_LOOKUP[alias] = member


def schedule_error_from_code(code: str) -> ScheduleError | None:
    """Resolve a raw code string (including legacy aliases) to ScheduleError."""

    return _CODE_LOOKUP.get(str(code).strip())


def require_schedule_error(code: str) -> ScheduleError:
    """Resolve a raw code string or raise KeyError."""

    resolved = schedule_error_from_code(code)
    if resolved is None:
        raise KeyError(f"Unknown schedule error code: {code!r}")
    return resolved


def manager_label_for_code(code: str, *, fallback_message: str = "") -> str:
    """Return the manager-facing label for a schedule error code."""

    resolved = schedule_error_from_code(code)
    if resolved is not None:
        if resolved is ScheduleError.BETWEEN_SHIFTS and "15" in fallback_message:
            return ScheduleError.UNION_TURNAROUND_15H.meta.manager_label
        return str(resolved.meta.manager_label)
    if code == ScheduleError.BETWEEN_SHIFTS.value and "15" in fallback_message:
        return ScheduleError.UNION_TURNAROUND_15H.meta.manager_label
    return str(code).replace("_", " ").title()
