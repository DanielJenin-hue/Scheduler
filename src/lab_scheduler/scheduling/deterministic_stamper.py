"""Sidecar deterministic 7-on/7-off stamper for the TWELVE_HOUR archetype.

This module is an isolated *sidecar*: it produces a finished schedule by stamping
the strict, contiguous 7-on/7-off master array onto every active roster line. It
intentionally shares **none** of the dynamic constraint-solver ("GamifiedSolver")
machinery used by the STANDARD archetype — no CP-SAT fill, no equity scoring, no
coverage-aggressor passes. The only cross-pathway dependencies are the shared
result/proof types (``AutoPilotRunResult`` / ``AutoPilotProof``) and the stable
FTE-reconciliation helpers, so that downstream persistence, HTML export, and
contract-tracking remain identical for both routes.

4-Line Staggered Pod architecture
---------------------------------
Full-time lines are locked onto a rigid 28-day cycle. For full-time line ``i`` the
master array is phase-shifted by ``(i % 4) * 7`` days and the on/off + Day/Night
token is decided by::

    cycle_week = ((day_index + (i % 4) * 7) % 28) // 7   # 0..3
    on_duty    = cycle_week in (0, 2)                    # 7-on / 7-off
    token      = D  (cycle_week == 0)  /  N  (cycle_week == 2)   # D/N lines

Because any four consecutive integers cover all residues mod 4, each contiguous
block of four same-designation D/N lines is spread across all four phases. That
guarantees exactly one Day and one Night seat per pod on every calendar day, so the
daily coverage is flat instead of oscillating. The stamper refuses to emit a
schedule whose full-time Day or Night tally ever collapses to zero.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import Dict, List, Mapping, Optional, Sequence, Set

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.scheduling.auto_generate import (
    AutoGenerateResult,
    EmployeeProfile,
    PlannedAssignment,
)
from lab_scheduler.scheduling.auto_pilot import (
    AutoPilotError,
    AutoPilotRunResult,
    assert_monday_block_start,
    build_auto_pilot_proof,
)
from lab_scheduler.scheduling.contract_payroll import FULLTIME_FTE_THRESHOLD
from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import (
    FTE_TOPUP_TEMPLATE_ID,
    MINIMUM_TOPUP_HOURS,
    _apply_six_week_surplus_drops,
    _daterange,
    _drop_excess_to_period_target,
    _inject_fte_topup,
    _resolve_contract_line_type,
    _resolve_twelve_hour_shift_template,
    _rotation_roster,
    period_contract_target_hours,
    six_week_scheduled_paid_hours,
)

__all__ = [
    "POD_CYCLE_DAYS",
    "POD_SIZE",
    "deterministic_stamper",
    "pod_stagger_offset_days",
]

# Rigid 4-line pod over a 28-day cycle. The on/off rhythm is 7-on / 7-off (a 14-day
# period), but the Day->Night block alternation doubles the cycle to 28 days, so the
# pod must be sized and phased on 28 days to keep both Day and Night seats covered.
POD_SIZE = 4
WEEK_DAYS = 7
POD_CYCLE_DAYS = POD_SIZE * WEEK_DAYS  # 28
ON_CYCLE_WEEKS = (0, 2)


def pod_stagger_offset_days(cohort_rank: int) -> int:
    """Phase offset for a line by its rank in the full-time cohort: ``(i % 4) * 7``."""

    return (cohort_rank % POD_SIZE) * WEEK_DAYS


def _pod_cycle_week(day_index: int, cohort_rank: int) -> int:
    """0-based week within the 28-day pod cycle after applying the rank offset."""

    return ((day_index + pod_stagger_offset_days(cohort_rank)) % POD_CYCLE_DAYS) // WEEK_DAYS


def _is_pod_work_day(day_index: int, cohort_rank: int) -> bool:
    return _pod_cycle_week(day_index, cohort_rank) in ON_CYCLE_WEEKS


def _pod_shift_code(day_index: int, cohort_rank: int, contract_line_type: str) -> str:
    """Day token for week 0, Night token for week 2 on rotating D/N lines."""

    if contract_line_type.upper() == "D/N":
        return "MORNING" if _pod_cycle_week(day_index, cohort_rank) == 0 else "NIGHT"
    return "MORNING"


def _resolve_template(
    code: str,
    by_code: Mapping[str, ShiftTemplateInfo],
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> ShiftTemplateInfo:
    template = by_code.get(code)
    if template is not None:
        return template
    return _resolve_twelve_hour_shift_template(shift_templates)


def _stamp_line_assignments(
    employee: EmployeeProfile,
    cohort_rank: int,
    *,
    cycle_anchor: date,
    period_start: date,
    period_end: date,
    by_code: Mapping[str, ShiftTemplateInfo],
    shift_templates: Mapping[str, ShiftTemplateInfo],
    blocked: Set[date],
) -> List[PlannedAssignment]:
    """Stamp one line's rigid 28-day pod pattern across the period."""

    contract_line_type = _resolve_contract_line_type(employee)
    line: List[PlannedAssignment] = []
    for assignment_date in _daterange(period_start, period_end):
        if assignment_date in blocked:
            continue
        day_index = (assignment_date - cycle_anchor).days
        if day_index < 0:
            continue
        if not _is_pod_work_day(day_index, cohort_rank):
            continue
        code = _pod_shift_code(day_index, cohort_rank, contract_line_type)
        template = _resolve_template(code, by_code, shift_templates)
        line.append(
            PlannedAssignment(
                employee_id=employee.id,
                shift_template_id=template.id,
                assignment_date=assignment_date,
            )
        )
    return line


def _coverage_token_for_code(code: str) -> str:
    normalized = str(code or "").strip().upper()
    if normalized in ("MORNING", "M", "DAY", "D", "DAY_12", "TWELVE_HOUR", "TWELV_HOUR"):
        return "D"
    if normalized in ("NIGHT", "N"):
        return "N"
    if normalized in ("EVENING", "E"):
        return "E"
    return ""


def _contiguous_runs(worked: Sequence[PlannedAssignment]) -> List[List[PlannedAssignment]]:
    """Split date-sorted worked shifts into contiguous (calendar-adjacent) on-runs."""

    runs: List[List[PlannedAssignment]] = []
    current: List[PlannedAssignment] = []
    for assignment in worked:
        if current and (assignment.assignment_date - current[-1].assignment_date).days == 1:
            current.append(assignment)
        else:
            if current:
                runs.append(current)
            current = [assignment]
    if current:
        runs.append(current)
    return runs


def _block_boundary_shifts(worked: Sequence[PlannedAssignment]) -> List[PlannedAssignment]:
    """Return only the boundary shifts of each on-run: Day 1 (first) and Day 7 (last).

    The FTE top-up token is FORBIDDEN from landing anywhere other than a block edge,
    so contiguity of the 7-on block is never broken in the interior. For a full 7-day
    run that is the first and last calendar day; a degenerate 1-day run yields a single
    boundary day.
    """

    boundary: List[PlannedAssignment] = []
    seen: Set[int] = set()
    for run in _contiguous_runs(worked):
        for candidate in (run[0], run[-1]):
            if id(candidate) not in seen:
                seen.add(id(candidate))
                boundary.append(candidate)
    return boundary


def _apply_fulltime_topup(
    line: Sequence[PlannedAssignment],
    *,
    employee: EmployeeProfile,
    cohort_rank: int,
    weeks_in_period: int,
    rules: JurisdictionRules,
    shift_templates: Mapping[str, ShiftTemplateInfo],
) -> List[PlannedAssignment]:
    """Replace one surplus shift with the FTE top-up token to land on the contract target.

    Hard placement rule: the ``T`` token may ONLY replace a block-boundary shift —
    Day 7 (last day) of a 7-on block or Day 1 (first day) of the next block. It is
    forbidden from any interior day, so the contiguous 7-on/7-off shape is preserved.

    Among the eligible boundary shifts the replaced one is preferentially a Day seat:
    Day coverage runs with surplus while Night seats are scarce (one per pod), so
    biasing the top-up onto a Day keeps the Night tally perfectly flat. The choice is
    scattered by ``cohort_rank`` so adjacent lines do not punch the same calendar date.
    """

    code_by_id = {tid: template.code for tid, template in shift_templates.items()}
    worked = sorted(
        (a for a in line if a.shift_template_id != FTE_TOPUP_TEMPLATE_ID),
        key=lambda a: a.assignment_date,
    )
    if not worked:
        return list(line)

    target_hours = period_contract_target_hours(
        fte=employee.fte, weeks_in_period=weeks_in_period, rules=rules
    )
    if six_week_scheduled_paid_hours(len(worked)) <= target_hours + MINIMUM_TOPUP_HOURS:
        return list(line)

    boundary = _block_boundary_shifts(worked)
    day_boundary = [
        a for a in boundary if _coverage_token_for_code(code_by_id.get(a.shift_template_id, "")) == "D"
    ]
    pool = day_boundary or boundary
    replace = pool[cohort_rank % len(pool)]
    replace_key = (replace.employee_id, replace.assignment_date, replace.shift_template_id)

    out: List[PlannedAssignment] = []
    for assignment in line:
        key = (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)
        if key == replace_key:
            out.append(
                PlannedAssignment(
                    employee_id=assignment.employee_id,
                    shift_template_id=FTE_TOPUP_TEMPLATE_ID,
                    assignment_date=assignment.assignment_date,
                    forced_clinical_ot=assignment.forced_clinical_ot,
                    overtime_compliance_bypassed=assignment.overtime_compliance_bypassed,
                    approved_stretch=assignment.approved_stretch,
                    clinical_floor_stretch=assignment.clinical_floor_stretch,
                    provisional_compliance=assignment.provisional_compliance,
                    contract_line_exception=assignment.contract_line_exception,
                    contract_line_exception_message=assignment.contract_line_exception_message,
                )
            )
        else:
            out.append(assignment)
    return out


def _enforce_full_time_coverage_balance(
    full_time_assignments: Sequence[PlannedAssignment],
    *,
    shift_templates: Mapping[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
) -> None:
    """Reject the block if the full-time Day or Night tally ever hits zero.

    Top-up tokens (``FTE_TOPUP_TEMPLATE_ID``) are not clinical seats and never count
    toward Day/Night coverage. A token is only enforced when the cohort produces it
    at all, so an all-Day roster is not failed for having no Night seats.
    """

    code_by_id = {tid: template.code for tid, template in shift_templates.items()}
    day_counts: Counter[date] = Counter()
    night_counts: Counter[date] = Counter()
    for assignment in full_time_assignments:
        if assignment.shift_template_id == FTE_TOPUP_TEMPLATE_ID:
            continue
        token = _coverage_token_for_code(code_by_id.get(assignment.shift_template_id, ""))
        if token == "D":
            day_counts[assignment.assignment_date] += 1
        elif token == "N":
            night_counts[assignment.assignment_date] += 1

    expects_day = sum(day_counts.values()) > 0
    expects_night = sum(night_counts.values()) > 0

    for assignment_date in _daterange(period_start, period_end):
        if expects_day and day_counts[assignment_date] == 0:
            raise AutoPilotError(
                "Deterministic stamper aborted: full-time Day coverage collapsed to 0 "
                f"on {assignment_date.isoformat()}. The 4-line pod stagger is not "
                "balanced for this roster."
            )
        if expects_night and night_counts[assignment_date] == 0:
            raise AutoPilotError(
                "Deterministic stamper aborted: full-time Night coverage collapsed to 0 "
                f"on {assignment_date.isoformat()}. The 4-line pod stagger is not "
                "balanced for this roster."
            )


def deterministic_stamper(
    *,
    rules: JurisdictionRules,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    shift_required_qualifications: Dict[str, Set[str]],
    employee_target_hours: Optional[Mapping[str, float]] = None,
    availability_blocked: Optional[Mapping[str, Set[date]]] = None,
) -> AutoPilotRunResult:
    """Stamp a deterministic 4-line-pod 7-on/7-off block and return a persist-ready result.

    The output is an :class:`AutoPilotRunResult`, identical in shape to the
    ``run_auto_pilot_full_block`` (GamifiedSolver) return value, so the AutoPilot
    trigger can persist and export it through the exact same shared code path.

    Raises ``AutoPilotError`` when the period does not begin on a Monday (master-array
    anchor) or when the full-time Day/Night daily coverage tally ever hits zero.
    """

    del shift_required_qualifications  # Stamper is deterministic; quals are validated at persist.

    cycle_anchor = assert_monday_block_start(period_start)

    result = AutoGenerateResult()
    result.schedule_archetype = ScheduleArchetype.TWELVE_HOUR.value
    if not employees or not shift_templates:
        proof = build_auto_pilot_proof(
            generate=result,
            rules=rules,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employee_target_hours=employee_target_hours,
            twelve_hour_mode=True,
        )
        return AutoPilotRunResult(generate=result, proof=proof)

    by_code = {template.code.upper(): template for template in shift_templates.values()}
    rotation_roster = _rotation_roster(employees)
    full_time_lines = [emp for emp in rotation_roster if emp.fte >= FULLTIME_FTE_THRESHOLD]
    part_time_lines = [emp for emp in rotation_roster if emp.fte < FULLTIME_FTE_THRESHOLD]

    def _blocked_for(employee: EmployeeProfile) -> Set[date]:
        if availability_blocked:
            return availability_blocked.get(employee.id, set())
        return set()

    full_time_assignments: List[PlannedAssignment] = []
    # Index-based stagger: offset = (i % 4) * 7 using rank in the full-time cohort.
    for i, employee in enumerate(full_time_lines):
        line = _stamp_line_assignments(
            employee,
            i,
            cycle_anchor=cycle_anchor,
            period_start=period_start,
            period_end=period_end,
            by_code=by_code,
            shift_templates=shift_templates,
            blocked=_blocked_for(employee),
        )
        line = _apply_fulltime_topup(
            line,
            employee=employee,
            cohort_rank=i,
            weeks_in_period=weeks_in_period,
            rules=rules,
            shift_templates=shift_templates,
        )
        full_time_assignments.extend(line)

    # Coverage-balance gate runs on the full-time cohort before the block is accepted.
    _enforce_full_time_coverage_balance(
        full_time_assignments,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
    )

    part_time_assignments: List[PlannedAssignment] = []
    for j, employee in enumerate(part_time_lines):
        line = _stamp_line_assignments(
            employee,
            j,
            cycle_anchor=cycle_anchor,
            period_start=period_start,
            period_end=period_end,
            by_code=by_code,
            shift_templates=shift_templates,
            blocked=_blocked_for(employee),
        )
        line = _apply_six_week_surplus_drops(
            line,
            employee_id=employee.id,
            fte=employee.fte,
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            scatter_index=j,
        )
        line = _drop_excess_to_period_target(
            line,
            employee_id=employee.id,
            fte=employee.fte,
            weeks_in_period=weeks_in_period,
            rules=rules,
            period_start=period_start,
            scatter_index=j,
        )
        line = _inject_fte_topup(
            line,
            employee=employee,
            employee_index=j,
            cycle_anchor=cycle_anchor,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            rules=rules,
            availability_blocked=availability_blocked,
        )
        part_time_assignments.extend(line)

    assignments = [*full_time_assignments, *part_time_assignments]
    result.assignments = assignments
    result.slots_filled = len(assignments)
    result.slots_total = len(assignments)
    result.deterministic_status = "GENERATED"
    result.schedule_status = "FINAL"
    result.coverage_complete = True

    proof = build_auto_pilot_proof(
        generate=result,
        rules=rules,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
        twelve_hour_mode=True,
    )
    return AutoPilotRunResult(generate=result, proof=proof)
