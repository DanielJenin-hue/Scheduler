from __future__ import annotations

import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

from lab_scheduler.models.employee import normalize_shift_band_code
from lab_scheduler.scheduling.breakroom_print import (
    BreakroomPostingContext,
    template_short_to_breakroom_token,
)

# Contractual Portage vacant-line roster (Lines 01–25). Supplemental Smooth Day
# Balance seats use synthetic Line 851+ labels and are excluded from breakroom export.
_PORTAGE_CONTRACT_ROSTER_LINE_COUNT = 25

VACANT_LINE_PATTERN = re.compile(r"Line\s+(\d+)", re.IGNORECASE)
SMOOTH_DAY_BALANCE_SLOT_ID_MARKER = "Smooth Day Balance -"
SUPPLEMENTAL_SEAT_INDEX_FLOOR = 850

TRIAGE_ESCALATED_CELL_TAG = "[UNFILLED - ESCALATED]"
OPTIONAL_UNSTAFFED_CELL_TAG = "Unstaffed - Optional"
SCHEDULE_METADATA_COLUMNS: Tuple[str, ...] = (
    "Employee",
    "employee_id",
    "fte",
    "contract_line_type",
)
EMPTY_SHIFT_DISPLAY = "—"
_VALID_ID_PREFIXES = ("emp-", "portage-")


def export_line_number_from_label(label: str) -> Optional[int]:
    match = VACANT_LINE_PATTERN.search(str(label or ""))
    if not match:
        return None
    return int(match.group(1))


def is_supplemental_ghost_export_row(row: Mapping[str, object]) -> bool:
    """Synthetic Smooth Day Balance rows (Line 26+) are not contractual roster lines."""

    line_number = export_line_number_from_label(str(row.get("Employee", "")))
    return line_number is not None and line_number > _PORTAGE_CONTRACT_ROSTER_LINE_COUNT


def is_optional_supplemental_triage_entry(entry: Mapping[str, object]) -> bool:
    """Triage for uncapped Smooth Day Balance seats is informational, not union risk."""

    slot_id = str(entry.get("slot_id") or "")
    if SMOOTH_DAY_BALANCE_SLOT_ID_MARKER in slot_id:
        return True
    seat_match = re.search(r"seat=(\d+)", slot_id)
    if seat_match and int(seat_match.group(1)) >= SUPPLEMENTAL_SEAT_INDEX_FLOOR:
        return True
    slot_label = str(entry.get("slot") or "")
    line_number = export_line_number_from_label(slot_label)
    return line_number is not None and line_number > _PORTAGE_CONTRACT_ROSTER_LINE_COUNT


def filter_breakroom_export_rows(
    schedule_rows: Sequence[Mapping[str, object]],
) -> List[Dict[str, object]]:
    """Drop supplemental ghost rows from breakroom/print views."""

    from lab_scheduler.scheduling.schedule_tallies import is_daily_tally_row

    return [
        dict(row)
        for row in schedule_rows
        if is_daily_tally_row(row) or not is_supplemental_ghost_export_row(row)
    ]

def _is_valid_employee_id(value: object) -> bool:
    text = str(value or "").strip()
    if not text or text in EMPTY_SHIFT_DISPLAY:
        return False
    if text in {"D", "E", "N", "M"}:
        return False
    return text.startswith(_VALID_ID_PREFIXES) or "-" in text


def shift_code_to_display_token(code: str) -> str:
    """Map DB/compliance shift codes to breakroom D/E/N tokens."""

    if str(code or "").strip().upper().startswith("TOPUP"):
        return "T"
    band = normalize_shift_band_code(code)
    if band == "MORNING":
        return "D"
    if band == "EVENING":
        return "E"
    if band == "NIGHT":
        return "N"
    return template_short_to_breakroom_token(code)


def template_record_to_display_token(template: Mapping[str, object]) -> str:
    code = str(template.get("code", "") or "")
    short = str(template.get("short", "") or "")
    if code:
        token = shift_code_to_display_token(code)
        if token:
            return token
    return template_short_to_breakroom_token(short)


def is_schedule_date_column(column: str, dates: Sequence[date]) -> bool:
    return column in {day.isoformat() for day in dates}


def resolve_contract_line_type(employee: Mapping[str, object]) -> str:
    explicit = str(employee.get("contract_line_type") or "").strip()
    if explicit:
        return explicit
    from lab_scheduler.scheduling.portage_template import portage_master_line_spec

    spec = portage_master_line_spec(_profile_from_mapping(employee))
    if spec is not None:
        return spec.contract_line_type
    qual = str(employee.get("qualifications") or "").upper()
    if "MLT" in qual:
        return "D/N"
    if "MLA" in qual:
        return "D/E"
    return ""


def _profile_from_mapping(employee: Mapping[str, object]):
    from lab_scheduler.scheduling.profiles import EmployeeProfile

    qual_ids = employee.get("qualification_ids")
    if isinstance(qual_ids, set):
        qset = qual_ids
    else:
        qset = set()
    return EmployeeProfile(
        id=str(employee.get("id", "")),
        full_name=str(employee.get("full_name", employee.get("Employee", ""))),
        fte=float(employee.get("fte", 1.0) or 1.0),
        qualification_ids=qset,
        contract_line_type=employee.get("contract_line_type"),
    )


def _roster_line_number(employee: Mapping[str, object]) -> Optional[int]:
    full_name = str(employee.get("full_name", employee.get("Employee", "")))
    match = VACANT_LINE_PATTERN.search(full_name)
    if match:
        return int(match.group(1))
    suffix = str(employee.get("id", "")).rsplit("-", 1)[-1]
    if suffix.isdigit():
        return int(suffix)
    return None


def _infer_role_code(employee: Mapping[str, object]) -> str:
    full_name = str(employee.get("full_name", employee.get("Employee", ""))).upper()
    if "MLT" in full_name or "mlt" in str(employee.get("id", "")).lower():
        return "MLT"
    if "MLA" in full_name or "mla" in str(employee.get("id", "")).lower():
        return "MLA"
    if "MLT" in str(employee.get("qualifications", "")).upper():
        return "MLT"
    return "MLA"


def _roster_dedupe_key(employee: Mapping[str, object]) -> str:
    line = _roster_line_number(employee)
    if line is not None:
        role = _infer_role_code(employee)
        contract = resolve_contract_line_type(employee) or "D/E"
        return f"{role}|{contract}|line-{line:02d}"
    return str(employee.get("id", employee.get("full_name", "")))


def dedupe_roster_for_schedule_export(
    employees: Sequence[Mapping[str, object]],
    *,
    assignment_counts: Optional[Mapping[str, int]] = None,
) -> List[Dict[str, object]]:
    """
    Collapse duplicate vacant-line roster rows to a single canonical employee.

    Prefers ``portage-*`` ids, then the row with the most assignments.
    """

    assignment_counts = assignment_counts or {}
    groups: Dict[str, List[Dict[str, object]]] = {}
    for employee in employees:
        row = dict(employee)
        key = _roster_dedupe_key(row)
        groups.setdefault(key, []).append(row)

    canonical: List[Dict[str, object]] = []
    for members in groups.values():
        if len(members) == 1:
            canonical.append(members[0])
            continue

        def sort_key(emp: Mapping[str, object]) -> Tuple[int, int, str]:
            emp_id = str(emp.get("id", ""))
            portage_pref = 0 if emp_id.startswith("portage-") else 1
            assigned = -int(assignment_counts.get(emp_id, 0))
            return (portage_pref, assigned, emp_id)

        canonical.append(sorted(members, key=sort_key)[0])
    return canonical


def _fallback_token_for_missing_template(template_id: str) -> Optional[str]:
    """Resolve a display token for a synthetic assignment whose template is not in the map."""

    identifier = str(template_id or "").lower()
    if "topup" in identifier or "top-up" in identifier or "top_up" in identifier:
        return "T"
    return None


def build_schedule_export_rows(
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    assignments: Sequence[Mapping[str, object]],
    templates: Mapping[str, Mapping[str, object]],
    *,
    blocked_map: Optional[Mapping[str, Mapping[date, str]]] = None,
    off_code_for_reason: Optional[object] = None,
    include_daily_tallies: bool = True,
) -> List[Dict[str, object]]:
    """
    One contiguous row per employee: metadata + ISO date columns with D/E/N tokens.
    """

    blocked_map = blocked_map or {}
    assignment_counts: Dict[str, int] = {}
    by_emp_date: Dict[Tuple[str, date], str] = {}

    for assignment in assignments:
        employee_id = str(assignment["employee_id"])
        assignment_date = assignment["assignment_date"]
        if not isinstance(assignment_date, date):
            assignment_date = date.fromisoformat(str(assignment_date))
        template_id = str(assignment["shift_template_id"])
        template = templates.get(template_id)
        if assignment.get("forced_clinical_ot"):
            token = "FORCED_CLINICAL_OT"
        elif template is not None:
            token = template_record_to_display_token(template)
        else:
            fallback_token = _fallback_token_for_missing_template(template_id)
            if fallback_token is None:
                continue
            token = fallback_token
        by_emp_date[(employee_id, assignment_date)] = token
        assignment_counts[employee_id] = assignment_counts.get(employee_id, 0) + 1

    roster = dedupe_roster_for_schedule_export(
        employees,
        assignment_counts=assignment_counts,
    )

    rows: List[Dict[str, object]] = []
    for employee in roster:
        employee_id = str(employee["id"])
        contract_line = resolve_contract_line_type(employee)
        row: Dict[str, object] = {
            "Employee": str(employee.get("full_name", employee.get("Employee", ""))),
            "employee_id": employee_id,
            "fte": float(employee.get("fte", 1.0) or 1.0),
            "contract_line_type": contract_line,
        }
        blocked_days = blocked_map.get(employee_id, {})
        for day in dates:
            reason = blocked_days.get(day)
            if reason and off_code_for_reason is not None:
                row[day.isoformat()] = off_code_for_reason(reason)
                continue
            token = by_emp_date.get((employee_id, day), "")
            row[day.isoformat()] = token if token else EMPTY_SHIFT_DISPLAY
        rows.append(row)

    if include_daily_tallies:
        from lab_scheduler.scheduling.schedule_tallies import tally_rows_from_employee_rows

        date_keys = [day.isoformat() for day in dates]
        rows.extend(tally_rows_from_employee_rows(rows, date_keys))

    return rows


def merge_fragmented_schedule_rows(
    rows: Sequence[Mapping[str, object]],
    dates: Sequence[date],
) -> List[Dict[str, object]]:
    """
    Merge export rows that were split across metadata vs shift columns.

    Combines rows sharing the same ``employee_id`` or the same ``Employee`` label.
    """

    date_keys = [day.isoformat() for day in dates]
    merged: Dict[str, Dict[str, object]] = {}

    def merge_key(row: Mapping[str, object]) -> str:
        name = str(row.get("Employee", "")).strip()
        line = VACANT_LINE_PATTERN.search(name)
        if line:
            role = "MLT" if "MLT" in name.upper() else "MLA"
            contract = str(row.get("contract_line_type", "") or "")
            if contract in {"", "—", "D", "E", "N", "M"}:
                contract = "D/E" if "D/E" in name.upper() else "D/N"
            return f"line:{role}|{contract}|{int(line.group(1)):02d}"
        employee_id = str(row.get("employee_id", "") or "").strip()
        if employee_id and employee_id not in {"", "—", "D", "E", "N", "M"}:
            return f"id:{employee_id}"
        return f"name:{name}"

    for row in rows:
        key = merge_key(row)
        if key not in merged:
            merged[key] = dict(row)
            continue
        target = merged[key]
        for field in ("employee_id", "fte", "contract_line_type", "Employee"):
            existing = target.get(field, "")
            incoming = row.get(field, "")
            if field == "employee_id":
                if _is_valid_employee_id(incoming) and not _is_valid_employee_id(existing):
                    target[field] = incoming
                elif _is_valid_employee_id(existing):
                    continue
                elif incoming and not _is_valid_employee_id(incoming):
                    continue
                else:
                    target[field] = incoming or existing
                continue
            existing_text = str(existing or "").strip()
            incoming_text = str(incoming or "").strip()
            if existing_text in {"", "—"} and incoming_text not in {"", "—", "D", "E", "N", "M"}:
                target[field] = incoming
        for date_key in date_keys:
            existing = template_short_to_breakroom_token(target.get(date_key, ""))
            incoming = template_short_to_breakroom_token(row.get(date_key, ""))
            if not existing and incoming:
                target[date_key] = incoming
            elif existing and incoming and existing != incoming:
                target[date_key] = incoming
    return list(merged.values())


def prepend_aggressive_fill_flags_to_export_rows(
    schedule_rows: Sequence[Mapping[str, object]],
    flags: Sequence[object],
) -> List[Dict[str, object]]:
    """Insert AGGRESSIVE_FILL_FLAGS block above schedule body rows."""

    from lab_scheduler.scheduling.coverage_aggressor import format_aggressive_fill_flags_csv_rows

    if not flags or not schedule_rows:
        return list(schedule_rows)

    flag_rows = format_aggressive_fill_flags_csv_rows(flags)
    sample = schedule_rows[0]
    fieldnames = list(sample.keys())
    padded_flags: List[Dict[str, object]] = []
    for flag_row in flag_rows:
        padded: Dict[str, object] = {key: "" for key in fieldnames}
        for key, value in flag_row.items():
            if key in padded:
                padded[key] = value
        padded_flags.append(padded)
    return [*padded_flags, *schedule_rows]


def is_aggressive_fill_flag_row(row: Mapping[str, object]) -> bool:
    employee = str(row.get("Employee", "")).strip()
    employee_id = str(row.get("employee_id", "")).strip()
    if employee == "AGGRESSIVE_FILL_FLAGS":
        return True
    if employee_id == "COVERAGE_AGGRESSOR_MODE":
        return True
    if employee.startswith("FLAG "):
        return True
    if employee.startswith("— END AGGRESSIVE_FILL_FLAGS"):
        return True
    return False


def load_triage_escalation_payload(path: Path) -> dict:
    """Load orchestrator triage JSON — no validation or constraint logic."""

    return json.loads(path.read_text(encoding="utf-8"))


def _triage_slot_label(entry: Mapping[str, object]) -> str:
    return str(entry.get("slot") or entry.get("slot_id") or "").strip()


def _triage_assignment_date(entry: Mapping[str, object]) -> str:
    return str(entry.get("date") or entry.get("assignment_date") or "").strip()


def _parse_triage_date(entry: Mapping[str, object]) -> Optional[date]:
    raw = _triage_assignment_date(entry)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _build_date_column_key_map(
    schedule_rows: Sequence[Mapping[str, object]],
    dates: Sequence[date],
) -> Dict[date, date | str]:
    """Map each schedule day to the dict key type used in ``schedule_rows``."""

    if not dates:
        return {}
    if any(any(day in row for day in dates) for row in schedule_rows):
        return {day: day for day in dates}
    return {day: day.isoformat() for day in dates}


def _read_schedule_cell(
    row: Mapping[str, object],
    day: date,
    date_column_keys: Mapping[date, date | str],
) -> object:
    primary = date_column_keys.get(day, day.isoformat())
    if primary in row:
        return row[primary]
    iso_key = day.isoformat()
    if iso_key in row:
        return row[iso_key]
    if day in row:
        return row[day]
    return EMPTY_SHIFT_DISPLAY


def _write_schedule_cell(
    row: Dict[str, object],
    day: date,
    value: str,
    date_column_keys: Mapping[date, date | str],
) -> None:
    row[date_column_keys.get(day, day.isoformat())] = value


def _schedule_cell_is_empty(value: object) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    if not text:
        return True
    if text in {EMPTY_SHIFT_DISPLAY, "—", "-", ".", "OFF", "NONE"}:
        return True
    if TRIAGE_ESCALATED_CELL_TAG in text:
        return False
    return False


_WORKED_SHIFT_CELL_TOKENS: frozenset[str] = frozenset(
    {"D", "E", "N", "M", "I", "V", "T", "MORNING", "EVENING", "NIGHT"}
)


def _cell_has_worked_shift(value: object) -> bool:
    """True when a grid cell carries a real shift assignment (not triage-only)."""
    text = str(value or "").strip()
    if _schedule_cell_is_empty(text):
        return False
    if TRIAGE_ESCALATED_CELL_TAG in text and " | " in text:
        text = text.split(" | ", 1)[0].strip()
    if _schedule_cell_is_empty(text):
        return False
    return text.upper() in _WORKED_SHIFT_CELL_TOKENS


def _sanitize_stale_triage_collisions(
    schedule_rows: Sequence[Mapping[str, object]],
    dates: Sequence[date],
) -> List[Dict[str, object]]:
    """Drop stale triage markers from cells that already carry shift assignments."""
    from lab_scheduler.scheduling.schedule_tallies import is_daily_tally_row

    date_column_keys = _build_date_column_key_map(schedule_rows, dates)
    cleaned: List[Dict[str, object]] = []
    for row in schedule_rows:
        if is_daily_tally_row(row):
            cleaned.append(dict(row))
            continue
        copy = dict(row)
        for day in dates:
            existing = _read_schedule_cell(copy, day, date_column_keys)
            text = str(existing or "").strip()
            if TRIAGE_ESCALATED_CELL_TAG not in text:
                continue
            if _cell_has_worked_shift(text):
                shift_part = text.split(" | ", 1)[0].strip()
                _write_schedule_cell(copy, day, shift_part, date_column_keys)
        cleaned.append(copy)
    return cleaned


def _apply_triage_marker_to_cell(existing: object, *, optional: bool = False) -> str:
    if optional:
        return OPTIONAL_UNSTAFFED_CELL_TAG
    existing_text = str(existing).strip() if existing is not None else ""
    if _schedule_cell_is_empty(existing_text):
        return TRIAGE_ESCALATED_CELL_TAG
    # Mutually exclusive: a filled shift and an unfilled-escalated tag cannot
    # coexist. When a real assignment is present, keep it and drop any stale tag.
    if TRIAGE_ESCALATED_CELL_TAG in existing_text:
        shift_part = existing_text.split(" | ", 1)[0].strip()
        if shift_part and not _schedule_cell_is_empty(shift_part):
            return shift_part
    return existing_text

def apply_triage_escalation_tags(
    schedule_rows: Sequence[Mapping[str, object]],
    triage_list: Sequence[Mapping[str, object]],
    dates: Sequence[date],
) -> List[Dict[str, object]]:
    """
    Paint triage escalation markers onto export rows before HTML rendering.

    Matches triage records to grid cells by ``slot`` label and ``date`` only.
    """

    from lab_scheduler.scheduling.schedule_tallies import is_daily_tally_row

    if not triage_list:
        return [dict(row) for row in schedule_rows]

    body_rows: List[Dict[str, object]] = [
        dict(row) for row in schedule_rows if not is_daily_tally_row(row)
    ]
    tally_rows: List[Dict[str, object]] = [
        dict(row) for row in schedule_rows if is_daily_tally_row(row)
    ]
    date_column_keys = _build_date_column_key_map(body_rows, dates)
    date_index = {day: day for day in dates}
    rows_by_slot = {
        str(row.get("Employee", "")).strip().casefold(): row for row in body_rows
    }

    for entry in triage_list:
        if is_optional_supplemental_triage_entry(entry):
            continue
        slot_label = _triage_slot_label(entry)
        triage_day = _parse_triage_date(entry)
        if not slot_label:
            logger.warning("Triage entry missing slot label; skipping: %s", entry)
            continue
        if triage_day is None:
            logger.warning(
                "Triage entry has invalid date for slot %s; skipping: %s",
                slot_label,
                entry,
            )
            continue
        if triage_day not in date_index:
            logger.warning(
                "Triage date %s for slot %s is outside export grid (%s..%s); skipping",
                triage_day.isoformat(),
                slot_label,
                dates[0].isoformat() if dates else "?",
                dates[-1].isoformat() if dates else "?",
            )
            continue

        row = rows_by_slot.get(slot_label.casefold())
        if row is None:
            logger.warning(
                "Triage slot %s on %s did not match any Employee row; appending synthetic row",
                slot_label,
                triage_day.isoformat(),
            )
            row = {
                "Employee": slot_label,
                "employee_id": "",
                "fte": 0.0,
                "contract_line_type": "",
            }
            for day in dates:
                _write_schedule_cell(row, day, EMPTY_SHIFT_DISPLAY, date_column_keys)
            body_rows.append(row)
            rows_by_slot[slot_label.casefold()] = row

        existing = _read_schedule_cell(row, triage_day, date_column_keys)
        if _cell_has_worked_shift(existing):
            # Stale triage entry: the slot is already filled in the live assignment grid.
            continue
        tagged = _apply_triage_marker_to_cell(existing)
        _write_schedule_cell(row, triage_day, tagged, date_column_keys)

    return _sanitize_stale_triage_collisions([*body_rows, *tally_rows], dates)


def render_breakroom_schedule_html(
    *,
    schedule_rows: Sequence[Mapping[str, object]],
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    period_start: date,
    period_end: date,
    week_count: int,
    triage_escalation_path: Optional[Path] = None,
    facility_name: str = "Northstar Medical Laboratory",
    period_name: str = "Schedule Block",
    compliance_verified_on: Optional[date] = None,
    schedule_archetype: str = "STANDARD",
    coverage_gaps_by_day: Optional[Mapping[date, int]] = None,
    paper_size: str = "legal",
    contract_target_hours_by_employee: Optional[Mapping[str, float]] = None,
    posting_context: BreakroomPostingContext | None = None,
) -> tuple[List[Dict[str, object]], str]:
    """
    Dumb breakroom renderer: ingest triage JSON state, tag matching cells, emit HTML.
    """

    from lab_scheduler.scheduling.breakroom_print import generate_breakroom_print_html

    triage_list: Sequence[Mapping[str, object]] = ()
    if triage_escalation_path is not None:
        triage_path = triage_escalation_path.resolve()
        if triage_path.is_file():
            triage_list = load_triage_escalation_payload(triage_path).get(
                "triage_list",
                (),
            )
        else:
            logger.error(
                "Triage escalation file missing on disk during breakroom render: path=%s",
                triage_path,
            )

    tagged_rows = apply_triage_escalation_tags(schedule_rows, triage_list, dates)
    breakroom_rows = filter_breakroom_export_rows(tagged_rows)
    from lab_scheduler.scheduling.night_streak_corrector import (
        validate_night_streaks_from_schedule_rows,
    )
    from lab_scheduler.scheduling.streak_validator import (
        validate_work_streaks_from_schedule_rows,
    )

    night_streak_violations = validate_night_streaks_from_schedule_rows(
        breakroom_rows,
        employees=employees,
        dates=dates,
    )
    work_streak_violations = validate_work_streaks_from_schedule_rows(
        breakroom_rows,
        employees=employees,
        dates=dates,
    )
    html = generate_breakroom_print_html(
        facility_name=facility_name,
        period_name=period_name,
        period_start=period_start,
        period_end=period_end,
        week_count=week_count,
        employees=employees,
        dates=dates,
        schedule_rows=breakroom_rows,
        compliance_verified_on=compliance_verified_on,
        night_streak_violations=night_streak_violations,
        work_streak_violations=work_streak_violations,
        schedule_archetype=schedule_archetype,
        coverage_gaps_by_day=coverage_gaps_by_day,
        paper_size=paper_size,
        contract_target_hours_by_employee=contract_target_hours_by_employee,
        posting_context=posting_context,
    )
    return breakroom_rows, html
