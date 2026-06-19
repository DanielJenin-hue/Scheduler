from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence

import pandas as pd

_METADATA_COLUMNS = frozenset({"Employee", "employee_id", "fte", "contract_line_type"})

SHIFT_TALLY_TOKENS: tuple[str, ...] = ("D", "E", "N")
# Weekday day headcount is not a fixed seat target — keep E/N for Portage clinical floor.
WEEKDAY_SHIFT_TARGETS: Dict[str, int] = {"D": 16, "E": 2, "N": 2}
WEEKEND_SHIFT_TARGETS: Dict[str, int] = {"D": 2, "E": 2, "N": 2}
WEEKEND_MORNING_TOTAL_CAP = WEEKEND_SHIFT_TARGETS["D"]
WEEKDAY_DAY_BALANCE_TOLERANCE = 1
DAILY_TALLY_ROW_NAMES: tuple[str, ...] = (
    "Total Days",
    "Total Evenings",
    "Total Nights",
)
LEGACY_DAILY_TALLY_ROW_NAMES: tuple[str, ...] = (
    "Total Days (Target: ±1 wd balance / 2 we)",
    "Total Evenings (Target: 2 wd / 2 we)",
    "Total Nights (Target: 2 wd / 2 we)",
)
ALL_DAILY_TALLY_ROW_NAMES: tuple[str, ...] = DAILY_TALLY_ROW_NAMES + LEGACY_DAILY_TALLY_ROW_NAMES
DAILY_TALLY_EMPLOYEE_IDS: tuple[str, ...] = (
    "__tally_total_days__",
    "__tally_total_evenings__",
    "__tally_total_nights__",
)


def shift_target_for_date(day: date, band: str) -> int:
    """Operational seat target for a calendar day and D/E/N band."""

    from lab_scheduler.policy.union_rules_portage import shift_target_for_portage_date

    return shift_target_for_portage_date(day, band)


def weekday_day_tally_status(count: int, weekday_counts: Sequence[int]) -> str:
    """
    UI status for weekday day-shift tallies.

    Weekday days must stay within ±1 of every other weekday in the view
    (e.g. if one day is 15, peers must be 14–16).
    """

    if not weekday_counts:
        return "tally-ok"
    lo = min(weekday_counts)
    hi = max(weekday_counts)
    if count >= hi - WEEKDAY_DAY_BALANCE_TOLERANCE and count <= lo + WEEKDAY_DAY_BALANCE_TOLERANCE:
        return "tally-ok"
    if count < hi - WEEKDAY_DAY_BALANCE_TOLERANCE:
        return "tally-short"
    return "tally-over"


def tally_band_for_row_label(label: str) -> str:
    normalized = str(label or "").strip()
    if normalized.startswith("Total Days") or normalized == DAILY_TALLY_ROW_NAMES[0]:
        return "D"
    if normalized.startswith("Total Evenings") or normalized == DAILY_TALLY_ROW_NAMES[1]:
        return "E"
    if normalized.startswith("Total Nights") or normalized == DAILY_TALLY_ROW_NAMES[2]:
        return "N"
    raise ValueError(f"Unknown tally label: {label}")


@dataclass(frozen=True, slots=True)
class DailyShiftTallies:
    """Per-date counts of Day, Evening, and Night assignments."""

    days: Dict[str, int]
    evenings: Dict[str, int]
    nights: Dict[str, int]

    def row_for(self, label: str, dates: Sequence[str]) -> Dict[str, object]:
        if label == DAILY_TALLY_ROW_NAMES[0]:
            values = self.days
            employee_id = DAILY_TALLY_EMPLOYEE_IDS[0]
        elif label == DAILY_TALLY_ROW_NAMES[1]:
            values = self.evenings
            employee_id = DAILY_TALLY_EMPLOYEE_IDS[1]
        elif label == DAILY_TALLY_ROW_NAMES[2]:
            values = self.nights
            employee_id = DAILY_TALLY_EMPLOYEE_IDS[2]
        else:
            raise ValueError(f"Unknown tally label: {label}")

        row: Dict[str, object] = {
            "Employee": label,
            "employee_id": employee_id,
            "fte": "",
            "contract_line_type": "",
        }
        for day_key in dates:
            row[day_key] = values.get(day_key, 0)
        return row


def is_daily_tally_employee_id(employee_id: object) -> bool:
    text = str(employee_id or "").strip()
    return text in DAILY_TALLY_EMPLOYEE_IDS


def is_daily_tally_row(row: Mapping[str, object]) -> bool:
    employee_id = row.get("employee_id")
    if is_daily_tally_employee_id(employee_id):
        return True
    employee_name = str(row.get("Employee", "")).strip()
    return employee_name in ALL_DAILY_TALLY_ROW_NAMES


def _normalize_shift_token(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value).strip().upper()
    if text in ("", "—", "-", "OFF", "NONE", "NAN", "."):
        return ""
    if text in {"M", "MORNING", "DAY"}:
        return "D"
    if text == "EVENING":
        return "E"
    if text == "NIGHT":
        return "N"
    if text in SHIFT_TALLY_TOKENS:
        return text
    return ""


def _date_columns(frame: pd.DataFrame, dates: Optional[Sequence[str]] = None) -> List[str]:
    if dates is not None:
        return [str(day) for day in dates]
    metadata = set(_METADATA_COLUMNS)
    return [column for column in frame.columns if column not in metadata]


def _employee_only_frame(frame: pd.DataFrame) -> pd.DataFrame:
    cleaned = frame.copy()
    if "employee_id" in cleaned.columns:
        cleaned = cleaned[~cleaned["employee_id"].apply(is_daily_tally_employee_id)]
    elif "Employee" in cleaned.columns:
        cleaned = cleaned[~cleaned["Employee"].isin(ALL_DAILY_TALLY_ROW_NAMES)]
    return cleaned


def calculate_daily_shift_tallies(
    assignments_dataframe: pd.DataFrame,
    *,
    dates: Optional[Sequence[str]] = None,
) -> DailyShiftTallies:
    """
    Count D/E/N tokens in each date column across employee rows.

    Ignores existing tally summary rows if they are already present.
    """

    employee_frame = _employee_only_frame(assignments_dataframe)
    date_columns = _date_columns(employee_frame, dates)

    days: Dict[str, int] = {}
    evenings: Dict[str, int] = {}
    nights: Dict[str, int] = {}

    for column in date_columns:
        day_count = 0
        evening_count = 0
        night_count = 0
        for value in employee_frame[column].tolist():
            token = _normalize_shift_token(value)
            if token == "D":
                day_count += 1
            elif token == "E":
                evening_count += 1
            elif token == "N":
                night_count += 1
        days[column] = day_count
        evenings[column] = evening_count
        nights[column] = night_count

    return DailyShiftTallies(days=days, evenings=evenings, nights=nights)


def count_shift_band_in_column(
    assignments_dataframe: pd.DataFrame,
    *,
    date_key: str,
    band: str,
) -> int:
    """Count D/E/N tokens in one date column across employee rows (ignores tally rows)."""

    token = band.strip().upper()
    if token not in SHIFT_TALLY_TOKENS:
        raise ValueError(f"Unknown tally band: {band}")
    employee_frame = _employee_only_frame(assignments_dataframe)
    if date_key not in employee_frame.columns:
        return 0
    return sum(
        1
        for value in employee_frame[date_key].tolist()
        if _normalize_shift_token(value) == token
    )


def build_daily_tally_rows(
    tallies: DailyShiftTallies,
    dates: Sequence[str],
) -> List[Dict[str, object]]:
    """Build the three non-editable summary rows for grid/export display."""

    return [
        tallies.row_for(DAILY_TALLY_ROW_NAMES[0], dates),
        tallies.row_for(DAILY_TALLY_ROW_NAMES[1], dates),
        tallies.row_for(DAILY_TALLY_ROW_NAMES[2], dates),
    ]


def append_daily_tally_rows(
    rows: Sequence[Mapping[str, object]],
    assignments_dataframe: pd.DataFrame,
    *,
    dates: Sequence[str],
) -> List[Dict[str, object]]:
    """
    Return employee rows plus tally rows without mutating the input sequence.
    """

    employee_rows = [dict(row) for row in rows if not is_daily_tally_row(row)]
    tallies = calculate_daily_shift_tallies(assignments_dataframe, dates=dates)
    return employee_rows + build_daily_tally_rows(tallies, dates)


def tally_rows_from_employee_rows(
    rows: Sequence[Mapping[str, object]],
    dates: Sequence[str],
) -> List[Dict[str, object]]:
    """Build tally rows from export/grid row dictionaries."""

    frame = pd.DataFrame(list(rows))
    tallies = calculate_daily_shift_tallies(frame, dates=dates)
    return build_daily_tally_rows(tallies, dates)


@dataclass(frozen=True, slots=True)
class PortageOperationalTallyViolation:
    assignment_date: date
    band: str
    actual: int
    target: int


def shift_band_from_template_code(code: object) -> str:
    normalized = str(code or "").strip().upper()
    if normalized in {"M", "MORNING", "DAY"}:
        return "D"
    if normalized == "EVENING":
        return "E"
    if normalized == "NIGHT":
        return "N"
    return ""


def find_portage_operational_tally_violations(
    assignments: Sequence[object],
    *,
    period_start: date,
    period_end: date,
    template_id_to_band: Mapping[str, str],
) -> List[PortageOperationalTallyViolation]:
    """
    Portage breakroom tallies require exactly 2 Evening and 2 Night seats per day
    (1 MLT + 1 MLA on the clinical floor). Returns day/band rows where the count
    differs from ``shift_target_for_portage_date``.
    """

    evening_counts: Dict[date, int] = {}
    night_counts: Dict[date, int] = {}
    for assignment in assignments:
        template_id = getattr(assignment, "shift_template_id", None)
        assignment_date = getattr(assignment, "assignment_date", None)
        if template_id is None or assignment_date is None:
            continue
        band = template_id_to_band.get(str(template_id), "")
        if band == "E":
            evening_counts[assignment_date] = evening_counts.get(assignment_date, 0) + 1
        elif band == "N":
            night_counts[assignment_date] = night_counts.get(assignment_date, 0) + 1

    violations: List[PortageOperationalTallyViolation] = []
    cursor = period_start
    while cursor <= period_end:
        for band, counts in (
            ("E", evening_counts),
            ("N", night_counts),
        ):
            target = shift_target_for_date(cursor, band)
            actual = counts.get(cursor, 0)
            if actual != target:
                violations.append(
                    PortageOperationalTallyViolation(
                        assignment_date=cursor,
                        band=band,
                        actual=actual,
                        target=target,
                    )
                )
        cursor += timedelta(days=1)
    return violations


def format_portage_tally_violation_summary(
    violations: Sequence[PortageOperationalTallyViolation],
    *,
    limit: int = 4,
) -> str:
    if not violations:
        return ""
    samples = ", ".join(
        f"{item.assignment_date.isoformat()} {item.band} {item.actual}/{item.target}"
        for item in violations[:limit]
    )
    suffix = "…" if len(violations) > limit else ""
    return f"{len(violations)} day-band mismatch(es) ({samples}{suffix})"
