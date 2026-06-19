from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import pandas as pd

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.engine.swap_controller import format_manual_assignment_warning
from lab_scheduler.policy.frame_bridge import (
    assignments_from_schedule_frame,
    normalize_grid_shift_token,
    schedule_frame_row_index_by_employee_id,
    template_id_from_short,
)
from lab_scheduler.policy.union_rules_portage import UNION_RULES_PORTAGE
from lab_scheduler.scheduling.auto_generate import EmployeeProfile, validate_assignment_change
from lab_scheduler.scheduling.breakroom_print import (
    ContractTrackingRow,
    compute_contract_tracking_row,
)
from lab_scheduler.scheduling.schedule_tallies import (
    DailyShiftTallies,
    calculate_daily_shift_tallies,
    is_daily_tally_employee_id,
    shift_target_for_date,
    weekday_day_tally_status,
)

SHORTFALL_ASSIST_TARGET_ID = "__shortfall_assist__"
WORKED_TOKENS = frozenset({"D", "E", "N", "M"})


@dataclass(frozen=True, slots=True)
class CellMutation:
    employee_id: str
    assignment_date: date
    previous_token: str
    new_token: str


@dataclass(frozen=True, slots=True)
class TallyOffTarget:
    assignment_date: date
    band: str
    count: int
    target: int

    @property
    def label(self) -> str:
        if self.count < self.target:
            status = "shortfall"
        elif self.count > self.target:
            status = "overstaff"
        else:
            status = "ok"
        return (
            f"{self.assignment_date.strftime('%a %b %d')} · "
            f"{_band_label(self.band)} · {self.count}/{self.target} ({status})"
        )


@dataclass
class PolicyViewModel:
    draft_frame: pd.DataFrame
    tallies: DailyShiftTallies
    off_target_cells: List[TallyOffTarget]
    contract_rows: Dict[str, ContractTrackingRow]
    biweekly_ot_risk: Dict[str, bool]
    cell_errors: Dict[str, str]
    pending_mutations: List[CellMutation]
    has_unpublished_changes: bool


def cell_mutation_to_dict(mutation: CellMutation) -> Dict[str, str]:
    return {
        "employee_id": mutation.employee_id,
        "assignment_date": mutation.assignment_date.isoformat(),
        "previous_token": mutation.previous_token,
        "new_token": mutation.new_token,
    }


def cell_mutation_from_dict(payload: Mapping[str, str]) -> CellMutation:
    return CellMutation(
        employee_id=str(payload["employee_id"]),
        assignment_date=date.fromisoformat(str(payload["assignment_date"])),
        previous_token=str(payload.get("previous_token", "")),
        new_token=str(payload.get("new_token", "")),
    )


def _band_label(band: str) -> str:
    return {"D": "Day", "E": "Evening", "N": "Night"}.get(band.upper(), band)


def compute_biweekly_ot_risk(
    row: Mapping[str, object],
    dates: Sequence[date],
    *,
    hours_per_shift: float = UNION_RULES_PORTAGE.hours_per_shift,
    biweekly_cap: float = UNION_RULES_PORTAGE.biweekly_normal_hours,
) -> bool:
    """True when any rolling 14-day window exceeds the bi-weekly normal cap."""

    if len(dates) < 2:
        return False

    daily_hours: Dict[date, float] = {}
    for day in dates:
        day_key = day.isoformat()
        token = normalize_grid_shift_token(row.get(day_key, ""))
        if token in WORKED_TOKENS:
            daily_hours[day] = hours_per_shift

    sorted_days = sorted(daily_hours)
    if not sorted_days:
        return False

    for index, start_day in enumerate(sorted_days):
        window_end = start_day + timedelta(days=13)
        total = 0.0
        for day in sorted_days[index:]:
            if day > window_end:
                break
            total += daily_hours[day]
        if total > biweekly_cap + 0.05:
            return True
    return False


def _off_target_cells_from_tallies(
    tallies: DailyShiftTallies,
    dates: Sequence[date],
) -> List[TallyOffTarget]:
    """Flag calendar days where D/E/N tallies miss operational targets."""

    off_targets: List[TallyOffTarget] = []
    weekday_dates = [day for day in dates if day.weekday() < 5]
    weekday_day_counts = [
        int(tallies.days.get(day.isoformat(), 0)) for day in weekday_dates
    ]

    for day in dates:
        date_key = day.isoformat()
        for band, values in (
            ("D", tallies.days),
            ("E", tallies.evenings),
            ("N", tallies.nights),
        ):
            count = int(values.get(date_key, 0))
            if band == "D" and day.weekday() < 5:
                if weekday_day_tally_status(count, weekday_day_counts) == "tally-ok":
                    continue
                lo = min(weekday_day_counts) if weekday_day_counts else count
                hi = max(weekday_day_counts) if weekday_day_counts else count
                target = (lo + hi) // 2
            else:
                target = shift_target_for_date(day, band)
                if count == target:
                    continue
            off_targets.append(
                TallyOffTarget(
                    assignment_date=day,
                    band=band,
                    count=count,
                    target=target,
                )
            )
    return off_targets


def _contract_rows_for_frame(
    frame: pd.DataFrame,
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
    week_count: int,
    schedule_archetype: str = "STANDARD",
    contract_target_hours: Optional[Mapping[str, float]] = None,
) -> Dict[str, ContractTrackingRow]:
    rows_by_id = {
        str(row.get("employee_id", "")): row
        for _, row in frame.iterrows()
        if not is_daily_tally_employee_id(row.get("employee_id"))
    }
    contract_rows: Dict[str, ContractTrackingRow] = {}
    for employee in employees:
        employee_id = str(employee.get("id") or employee.get("employee_id") or "")
        if not employee_id:
            continue
        row = rows_by_id.get(employee_id)
        if row is None:
            continue
        contract_target = (
            float(contract_target_hours[employee_id])
            if contract_target_hours and employee_id in contract_target_hours
            else None
        )
        contract_rows[employee_id] = compute_contract_tracking_row(
            fte=float(employee.get("fte", 1.0) or 1.0),
            week_count=week_count,
            row=row,
            dates=dates,
            contract_line_type=str(employee.get("contract_line_type", "") or ""),
            schedule_archetype=schedule_archetype,
            contract_target_hours=contract_target,
        )
    return contract_rows


def _biweekly_risk_for_frame(
    frame: pd.DataFrame,
    *,
    employees: Sequence[Mapping[str, object]],
    dates: Sequence[date],
) -> Dict[str, bool]:
    rows_by_id = {
        str(row.get("employee_id", "")): row
        for _, row in frame.iterrows()
        if not is_daily_tally_employee_id(row.get("employee_id"))
    }
    risk: Dict[str, bool] = {}
    for employee in employees:
        employee_id = str(employee.get("id") or employee.get("employee_id") or "")
        if not employee_id:
            continue
        row = rows_by_id.get(employee_id)
        if row is None:
            continue
        risk[employee_id] = compute_biweekly_ot_risk(row, dates)
    return risk


def _record_pending_mutation(
    pending: List[CellMutation],
    mutation: CellMutation,
) -> List[CellMutation]:
    updated = [
        item
        for item in pending
        if not (
            item.employee_id == mutation.employee_id
            and item.assignment_date == mutation.assignment_date
        )
    ]
    if mutation.previous_token != mutation.new_token:
        updated.append(mutation)
    return updated


class SchedulePolicyEngine:
    """Central policy layer for staged grid mutations and derived view models."""

    def derive_view_model(
        self,
        draft_frame: pd.DataFrame,
        *,
        employees: Sequence[Mapping[str, object]],
        dates: Sequence[date],
        week_count: int,
        pending_mutations: Optional[Sequence[CellMutation]] = None,
        cell_errors: Optional[Mapping[str, str]] = None,
        schedule_archetype: str = "STANDARD",
        contract_target_hours: Optional[Mapping[str, float]] = None,
    ) -> PolicyViewModel:
        date_keys = [day.isoformat() for day in dates]
        tallies = calculate_daily_shift_tallies(draft_frame, dates=date_keys)
        pending = list(pending_mutations or [])
        return PolicyViewModel(
            draft_frame=draft_frame,
            tallies=tallies,
            off_target_cells=_off_target_cells_from_tallies(tallies, dates),
            contract_rows=_contract_rows_for_frame(
                draft_frame,
                employees=employees,
                dates=dates,
                week_count=week_count,
                schedule_archetype=schedule_archetype,
                contract_target_hours=contract_target_hours,
            ),
            biweekly_ot_risk=_biweekly_risk_for_frame(
                draft_frame,
                employees=employees,
                dates=dates,
            ),
            cell_errors=dict(cell_errors or {}),
            pending_mutations=pending,
            has_unpublished_changes=bool(pending),
        )

    def apply_mutations(
        self,
        *,
        draft_frame: pd.DataFrame,
        edited_frame: pd.DataFrame,
        employees: Sequence[Mapping[str, object]],
        dates: Sequence[date],
        templates: Mapping[str, Mapping[str, object]],
        template_info: Mapping[str, ShiftTemplateInfo],
        shift_quals: Mapping[str, Set[str]],
        rules: JurisdictionRules,
        period_start: date,
        period_end: date,
        weeks_in_period: int,
        employee_target_hours: Optional[Mapping[str, float]] = None,
        availability_blocked: Optional[Mapping[str, Set[date]]] = None,
        blocked_map: Optional[Mapping[str, Mapping[date, str]]] = None,
        pending_mutations: Optional[Sequence[CellMutation]] = None,
        cell_errors: Optional[Mapping[str, str]] = None,
        profiles_by_id: Optional[Mapping[str, EmployeeProfile]] = None,
        is_availability_off_code: Optional[Callable[[str], bool]] = None,
        reason_to_off_code: Optional[Callable[[str], str]] = None,
        contract_target_hours: Optional[Mapping[str, float]] = None,
        locked_cells: Optional[Set[Tuple[str, date]]] = None,
        enforce_assignment_rules: bool = True,
    ) -> Tuple[PolicyViewModel, bool, List[str]]:
        """
        Validate and apply staged shift edits.

        Returns (view_model, any_applied, toast_messages).
        """

        working = edited_frame.copy()
        baseline = draft_frame.copy()
        errors: Dict[str, str] = dict(cell_errors or {})
        pending = list(pending_mutations or [])
        toast_messages: List[str] = []
        any_applied = False
        blocked_map = blocked_map or {}
        locked_cells = locked_cells or set()

        profiles = profiles_by_id or {}
        scheduled = assignments_from_schedule_frame(
            working,
            employees=employees,
            dates=dates,
            templates=templates,
        )
        row_by_employee = schedule_frame_row_index_by_employee_id(working)

        for employee in employees:
            employee_id = str(employee["id"])
            row_idx = row_by_employee.get(employee_id)
            if row_idx is None:
                continue
            profile = profiles.get(employee_id)
            if profile is None:
                continue

            for assignment_date in dates:
                col = assignment_date.isoformat()
                reason = blocked_map.get(employee_id, {}).get(assignment_date)
                if reason and reason_to_off_code is not None:
                    locked_code = reason_to_off_code(reason)
                    attempted = normalize_grid_shift_token(working.at[row_idx, col])
                    if attempted != locked_code:
                        working.at[row_idx, col] = locked_code
                        cell_key = f"{employee['full_name']}|{assignment_date.isoformat()}"
                        errors[cell_key] = f"Approved time off ({reason}) — cell locked."
                    baseline.at[row_idx, col] = locked_code
                    continue

                old_short = normalize_grid_shift_token(baseline.at[row_idx, col])
                new_short = normalize_grid_shift_token(working.at[row_idx, col])
                if old_short == new_short:
                    continue

                if is_availability_off_code and is_availability_off_code(new_short):
                    working.at[row_idx, col] = old_short
                    continue

                from lab_scheduler.scheduling.streak_validator import is_worked_schedule_cell

                if (employee_id, assignment_date) in locked_cells and is_worked_schedule_cell(
                    old_short
                ):
                    working.at[row_idx, col] = old_short
                    cell_key = f"{employee['full_name']}|{assignment_date.isoformat()}"
                    errors[cell_key] = "Cell is locked — right-click any day in the week to unlock."
                    continue

                new_shift_id = template_id_from_short(templates, new_short)
                if new_short and new_shift_id is None:
                    if enforce_assignment_rules:
                        working.at[row_idx, col] = old_short
                        cell_key = f"{employee['full_name']}|{assignment_date.isoformat()}"
                        errors[cell_key] = f"Unknown shift code '{new_short}'."
                        continue
                    new_shift_id = None

                if enforce_assignment_rules:
                    violation = validate_assignment_change(
                        rules=rules,
                        period_start=period_start,
                        period_end=period_end,
                        weeks_in_period=weeks_in_period,
                        employee=profile,
                        all_assignments=scheduled,
                        shift_templates=template_info,
                        shift_required_qualifications=shift_quals,
                        assignment_date=assignment_date,
                        new_shift_template_id=new_shift_id,
                        employee_target_hours=employee_target_hours,
                        availability_blocked=availability_blocked,
                    )
                    if violation:
                        friendly = format_manual_assignment_warning(
                            employee_name=str(employee["full_name"]),
                            contract_line_type=employee.get("contract_line_type"),
                            assignment_date=assignment_date,
                            shift_type=new_short or "—",
                            violation=violation,
                        )
                        working.at[row_idx, col] = old_short
                        cell_key = f"{employee['full_name']}|{assignment_date.isoformat()}"
                        errors[cell_key] = friendly
                        continue

                baseline.at[row_idx, col] = new_short
                scheduled = _apply_assignment_delta(
                    scheduled,
                    employee_id=employee_id,
                    employee_name=str(employee["full_name"]),
                    assignment_date=assignment_date,
                    new_shift_id=new_shift_id,
                )
                pending = _record_pending_mutation(
                    pending,
                    CellMutation(
                        employee_id=employee_id,
                        assignment_date=assignment_date,
                        previous_token=old_short,
                        new_token=new_short,
                    ),
                )
                cell_key = f"{employee['full_name']}|{assignment_date.isoformat()}"
                errors.pop(cell_key, None)
                any_applied = True

        view_model = self.derive_view_model(
            baseline,
            employees=employees,
            dates=dates,
            week_count=weeks_in_period,
            pending_mutations=pending,
            cell_errors=errors,
            contract_target_hours=contract_target_hours,
        )
        view_model = replace(view_model, draft_frame=baseline)
        return view_model, any_applied, toast_messages


def _apply_assignment_delta(
    scheduled: List[ScheduledShift],
    *,
    employee_id: str,
    employee_name: str,
    assignment_date: date,
    new_shift_id: Optional[str],
) -> List[ScheduledShift]:
    from lab_scheduler.compliance.engine import ScheduledShift

    filtered = [
        assignment
        for assignment in scheduled
        if not (
            assignment.employee_id == employee_id
            and assignment.assignment_date == assignment_date
        )
    ]
    if new_shift_id:
        filtered.append(
            ScheduledShift(
                employee_id=employee_id,
                employee_name=employee_name,
                assignment_date=assignment_date,
                shift_template_id=new_shift_id,
            )
        )
    return filtered


PersistResult = Tuple[bool, str]


def flush_pending_mutations(
    pending_mutations: Sequence[CellMutation],
    *,
    persist_cell_change: Callable[..., PersistResult],
) -> Tuple[int, List[str]]:
    """
    Apply queued mutations through ``persist_cell_change``.

    ``persist_cell_change`` must accept keyword args:
    employee_id, assignment_date, previous_token, new_token
    and return (success, message).
    """

    applied = 0
    errors: List[str] = []
    for mutation in pending_mutations:
        ok, message = persist_cell_change(
            employee_id=mutation.employee_id,
            assignment_date=mutation.assignment_date,
            previous_token=mutation.previous_token,
            new_token=mutation.new_token,
        )
        if ok:
            applied += 1
        elif message:
            errors.append(message)
    return applied, errors
