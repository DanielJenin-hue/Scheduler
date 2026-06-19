from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.engine.demand import fatigue_guardrail_violation
from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code
from lab_scheduler.scheduling.anchor_tiers import AnchorTier, anchor_tier_for_cell
from lab_scheduler.scheduling.profiles import EmployeeProfile

_WORKED_BANDS = frozenset({"D", "E", "N"})


def _cell_has_worked_assignment(
    assignments: Sequence[object],
    *,
    employee_id: str,
    assignment_date: date,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> bool:
    for assignment in assignments:
        if getattr(assignment, "employee_id", "") != employee_id:
            continue
        if getattr(assignment, "assignment_date", None) != assignment_date:
            continue
        template = shift_templates.get(getattr(assignment, "shift_template_id", ""))
        if template is None:
            continue
        if shift_band_from_template_code(template.code) in _WORKED_BANDS:
            return True
    return False


@dataclass(frozen=True, slots=True)
class PostPassGuard:
    """Protect frozen master-line cells, manager locks, night anchors, and fatigue."""

    frozen_master_cells: Set[tuple[str, date]]
    manager_locked_cells: Set[tuple[str, date]] = field(default_factory=set)
    employees: Sequence[EmployeeProfile] = field(default_factory=tuple)
    period_start: date | None = None

    def _employee(self, employee_id: str) -> EmployeeProfile | None:
        return next((profile for profile in self.employees if profile.id == employee_id), None)

    def anchor_tier(
        self,
        *,
        employee_id: str,
        assignment_date: date,
        assignments: Sequence[object],
        shift_templates: Mapping[str, ShiftTemplateInfo],
    ) -> AnchorTier:
        employee = self._employee(employee_id)
        if employee is None or self.period_start is None:
            return AnchorTier.SOFT
        return anchor_tier_for_cell(
            employee,
            assignment_date,
            self.period_start,
            manager_locked_cells=self.manager_locked_cells,
            assignments=assignments,
            shift_templates=shift_templates,
        )

    def blocks_anchor_modification(
        self,
        assignments: Sequence[object],
        *,
        employee_id: str,
        assignment_date: date,
        shift_templates: Mapping[str, ShiftTemplateInfo],
        minimum_tier: AnchorTier = AnchorTier.NIGHT_ANCHOR,
    ) -> bool:
        return self.anchor_tier(
            employee_id=employee_id,
            assignment_date=assignment_date,
            assignments=assignments,
            shift_templates=shift_templates,
        ) >= minimum_tier

    def blocks_worked_cell_modification(
        self,
        assignments: Sequence[object],
        *,
        employee_id: str,
        assignment_date: date,
        shift_templates: Mapping[str, ShiftTemplateInfo],
    ) -> bool:
        if self.blocks_anchor_modification(
            assignments,
            employee_id=employee_id,
            assignment_date=assignment_date,
            shift_templates=shift_templates,
        ):
            return True
        if (employee_id, assignment_date) not in self.manager_locked_cells:
            return False
        return _cell_has_worked_assignment(
            assignments,
            employee_id=employee_id,
            assignment_date=assignment_date,
            shift_templates=shift_templates,
        )

    def allows_assignment(
        self,
        *,
        assignments: Sequence[object],
        employee_id: str,
        assignment_date: date,
        shift_template_id: str,
        shift_templates: Mapping[str, ShiftTemplateInfo],
        employees: Sequence[EmployeeProfile],
        qual_codes: Mapping[str, str],
        replace_existing: bool = False,
    ) -> bool:
        if (employee_id, assignment_date) in self.frozen_master_cells and not replace_existing:
            return False

        if (employee_id, assignment_date) in self.manager_locked_cells:
            if replace_existing and _cell_has_worked_assignment(
                assignments,
                employee_id=employee_id,
                assignment_date=assignment_date,
                shift_templates=shift_templates,
            ):
                return False

        work_dates = {
            getattr(assignment, "assignment_date")
            for assignment in assignments
            if getattr(assignment, "employee_id", "") == employee_id
        }
        if not replace_existing:
            work_dates.discard(assignment_date)

        employee = next((profile for profile in employees if profile.id == employee_id), None)
        modified = bool(getattr(employee, "modified_work_schedule", False)) if employee else False
        violation = fatigue_guardrail_violation(
            work_dates,
            assignment_date,
            modified_work_schedule=modified,
        )
        return violation is None


def should_bypass_post_cpsat_healing(
    *,
    coverage_gap_count: int,
    clinical_seats_locked: bool,
    compliance_first: bool = True,
) -> bool:
    """
    Skip post-CP-SAT healing when compliance-first mode is active, or when required
    coverage and E/N clinical seats are already satisfied on the deduped snapshot.
    """

    if compliance_first:
        return True
    return coverage_gap_count == 0 and clinical_seats_locked
