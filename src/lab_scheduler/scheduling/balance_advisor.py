"""Swap suggestions for tally variance (Layer 3 soft cells only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Mapping, Sequence, Set

from lab_scheduler.scheduling.anchor_tiers import AnchorTier, anchor_tier_for_cell
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code


@dataclass(frozen=True, slots=True)
class SwapOption:
    summary: str
    from_employee_id: str
    to_employee_id: str
    assignment_date: date
    shift_band: str


def _assignment_band_map(
    assignments: Sequence[object],
    shift_templates: Mapping[str, object],
) -> Dict[tuple[str, date], str]:
    bands: Dict[tuple[str, date], str] = {}
    for assignment in assignments:
        employee_id = getattr(assignment, "employee_id", "")
        assignment_date = getattr(assignment, "assignment_date", None)
        if not employee_id or assignment_date is None:
            continue
        template = shift_templates.get(getattr(assignment, "shift_template_id", ""))
        if template is None:
            continue
        bands[(employee_id, assignment_date)] = shift_band_from_template_code(template.code)
    return bands


def suggest_swaps_for_tally_variance(
    *,
    band: str,
    assignment_date: date,
    assignments: Sequence[object],
    employees: Sequence[EmployeeProfile],
    shift_templates: Mapping[str, object],
    period_start: date,
    manager_locked_cells: Set[tuple[str, date]] | None = None,
    max_options: int = 3,
) -> List[SwapOption]:
    """
    Propose soft D/E moves that restore daily band balance without touching N anchors.
    """

    if band == "N":
        return []

    band = band.upper()
    employee_by_id = {employee.id: employee for employee in employees}
    bands = _assignment_band_map(assignments, shift_templates)
    donors: list[tuple[str, str]] = []
    receivers: list[str] = []

    for employee in employees:
        key = (employee.id, assignment_date)
        cell_band = bands.get(key)
        tier = anchor_tier_for_cell(
            employee,
            assignment_date,
            period_start,
            manager_locked_cells=manager_locked_cells,
            assignments=assignments,
            shift_templates=shift_templates,
        )
        if tier >= AnchorTier.NIGHT_ANCHOR:
            continue
        if cell_band == band:
            donors.append((employee.id, cell_band))
        elif cell_band is None:
            receivers.append(employee.id)

    options: List[SwapOption] = []
    for donor_id, _donor_band in donors:
        for receiver_id in receivers:
            if donor_id == receiver_id:
                continue
            donor = employee_by_id.get(donor_id)
            receiver = employee_by_id.get(receiver_id)
            if donor is None or receiver is None:
                continue
            if (
                anchor_tier_for_cell(
                    receiver,
                    assignment_date,
                    period_start,
                    manager_locked_cells=manager_locked_cells,
                )
                >= AnchorTier.CATALOG_DAY
            ):
                continue
            options.append(
                SwapOption(
                    summary=(
                        f"Move {band} on {assignment_date.isoformat()} "
                        f"from {donor.full_name} to {receiver.full_name}"
                    ),
                    from_employee_id=donor_id,
                    to_employee_id=receiver_id,
                    assignment_date=assignment_date,
                    shift_band=band,
                )
            )
            if len(options) >= max_options:
                return options
    return options
