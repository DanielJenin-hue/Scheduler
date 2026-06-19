from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from itertools import combinations
from typing import Callable, Dict, Mapping, Optional, Sequence, Set, Tuple

from lab_scheduler.audit.compliance import ComplianceConflict
from lab_scheduler.scheduling.provisional_state_cleanup import clear_provisional_stretch_state
from lab_scheduler.scheduling.provisional_compliance import (
    ProvisionalAssignment,
    approved_contract_line_exception_system_note,
    approved_stretch_system_note,
    contract_line_exception_system_note,
    provisional_stretch_system_note,
)
from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo, evaluate_schedule
from lab_scheduler.compliance.jurisdictions import JurisdictionRules
from lab_scheduler.workers.logic_worker import LogicWorkerFailure, LogicWorkerRejection, require_monday_block_start
from lab_scheduler.engine.constraints import CoverageTierTarget, compute_coverage_success_rate_pct
from lab_scheduler.scheduling.persist_validation import (
    find_core_persist_violations,
    log_core_persist_violations,
)
from lab_scheduler.scheduling.strategies import ScheduleArchetype, normalize_archetype

from lab_scheduler.legacy.auto_generate import (
    AutoGenerateResult,
    ClinicalShortageError,
    DeterministicScheduleFailure,
    EmployeeProfile,
    ImmediateClinicalFailure,
    PlannedAssignment,
    auto_generate_schedule,
    validate_generated_schedule,
)


class AutoPilotError(Exception):
    """Raised when Auto-Pilot cannot safely generate a full schedule block."""

    def __init__(
        self,
        message: str,
        *,
        conflicts: Optional[Sequence[ComplianceConflict]] = None,
        conflict_report_path: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.conflicts = list(conflicts or ())
        self.conflict_report_path = conflict_report_path or ""


@dataclass(frozen=True, slots=True)
class AutoPilotProof:
    block_start_monday: date
    week_count: int
    lines_populated: int
    slots_filled: int
    slots_total: int
    total_statutory_ot_hours: float
    compliance_error_count: int
    compliance_warning_count: int
    coverage_complete: bool = True
    coverage_success_rate_pct: float = 100.0
    coverage_gap_count: int = 0
    optional_coverage_gap_count: int = 0
    provisional_override_count: int = 0
    schedule_status: str = "FINAL"

    @property
    def legal_compliance_pct(self) -> float:
        if self.compliance_error_count == 0:
            return 100.0
        return max(0.0, 100.0 - (self.compliance_error_count / max(self.slots_total, 1)) * 100.0)

    def success_message(self) -> str:
        week_label = f"{self.week_count}-Week"
        if self.total_statutory_ot_hours < 0.01:
            ot_phrase = "0 Overtime Hours Logged"
        else:
            ot_phrase = f"{self.total_statutory_ot_hours:.1f} Overtime Hours Logged"
        if self.compliance_error_count == 0:
            compliance_phrase = "100% Legal Compliance Cleared"
        else:
            compliance_phrase = f"{self.legal_compliance_pct:.0f}% Legal Compliance"
        if self.provisional_override_count > 0:
            compliance_phrase = (
                f"{compliance_phrase} · "
                f"{self.provisional_override_count} Provisional Override(s) Pending Approval"
            )
        coverage_phrase = (
            "Coverage Targets Met"
            if self.coverage_complete and self.optional_coverage_gap_count <= 0
            else (
                f"Required Coverage Met ({self.optional_coverage_gap_count} optional supplemental gap(s))"
                if self.coverage_complete
                else (
                    f"{self.coverage_gap_count} required coverage gap(s) remain "
                    f"({self.coverage_success_rate_pct:.0f}% slot fill)"
                    if self.coverage_gap_count > 0
                    else f"Coverage {self.coverage_success_rate_pct:.0f}% ({self.coverage_gap_count} gap(s))"
                )
            )
        )
        return (
            f"Success: {week_label} Block Generated. "
            f"{self.lines_populated} Lines Populated. "
            f"{ot_phrase}. "
            f"{compliance_phrase}. "
            f"{coverage_phrase}."
        )


@dataclass(frozen=True, slots=True)
class AutoPilotRunResult:
    generate: AutoGenerateResult
    proof: AutoPilotProof


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def assert_monday_block_start(period_start: date) -> date:
    try:
        return require_monday_block_start(period_start)
    except LogicWorkerRejection as exc:
        raise AutoPilotError(str(exc)) from exc


def build_auto_pilot_proof(
    *,
    generate: AutoGenerateResult,
    rules: JurisdictionRules,
    employees: Sequence[EmployeeProfile],
    shift_templates: Dict[str, ShiftTemplateInfo],
    period_start: date,
    period_end: date,
    weeks_in_period: int,
    employee_target_hours: Optional[Mapping[str, float]] = None,
    lines_populated: Optional[int] = None,
    twelve_hour_mode: bool = False,
) -> AutoPilotProof:
    block_start = assert_monday_block_start(period_start)
    if twelve_hour_mode:
        return AutoPilotProof(
            block_start_monday=block_start,
            week_count=weeks_in_period,
            lines_populated=(
                len({assignment.employee_id for assignment in generate.assignments})
                if lines_populated is None
                else lines_populated
            ),
            slots_filled=generate.slots_filled,
            slots_total=generate.slots_total,
            total_statutory_ot_hours=0.0,
            compliance_error_count=0,
            compliance_warning_count=0,
            coverage_complete=True,
            coverage_success_rate_pct=100.0,
            coverage_gap_count=0,
            optional_coverage_gap_count=0,
            provisional_override_count=len(generate.provisional_assignments),
            schedule_status=generate.schedule_status,
        )
    emp_lookup = {employee.id: employee.full_name for employee in employees}
    scheduled = [
        ScheduledShift(
            employee_id=assignment.employee_id,
            employee_name=emp_lookup.get(assignment.employee_id, assignment.employee_id),
            assignment_date=assignment.assignment_date,
            shift_template_id=assignment.shift_template_id,
        )
        for assignment in generate.assignments
    ]
    employee_dicts = [
        {"id": employee.id, "full_name": employee.full_name, "fte": employee.fte}
        for employee in employees
    ]
    report = evaluate_schedule(
        rules,
        employees=employee_dicts,
        assignments=scheduled,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
    )
    total_ot = round(sum(summary.statutory_overtime_hours for summary in report.labor_summaries), 2)
    return AutoPilotProof(
        block_start_monday=block_start,
        week_count=weeks_in_period,
        lines_populated=lines_populated if lines_populated is not None else len(generate.assignments),
        slots_filled=generate.slots_filled,
        slots_total=generate.slots_total,
        total_statutory_ot_hours=total_ot,
        compliance_error_count=report.error_count,
        compliance_warning_count=report.warning_count,
        coverage_complete=generate.coverage_complete,
        coverage_success_rate_pct=compute_coverage_success_rate_pct(
            generate.coverage_tier_results
        ),
        coverage_gap_count=generate.coverage_gap_count,
        optional_coverage_gap_count=generate.optional_coverage_gap_count,
        provisional_override_count=len(generate.provisional_assignments),
        schedule_status=generate.schedule_status,
    )


def run_auto_pilot_full_block(
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
    bypass_compliance_rules: bool = False,
    coverage_targets: Optional[Sequence[CoverageTierTarget]] = None,
    require_master_compliance: bool = False,
    coverage_aggressor_mode: bool = False,
    strict_complete_block: bool = True,
    emit_triage: bool = False,
    conn: Optional[sqlite3.Connection] = None,
    tenant_id: Optional[str] = None,
    schedule_period_id: Optional[str] = None,
    project_root: Optional[Path] = None,
    clear_provisional_state: bool = True,
    archetype: str = "STANDARD",
    progress_callback: Optional[Callable[[str], None]] = None,
    enable_fairness_rerun: bool = True,
    portage_scheduling_policy: Optional["PortageSchedulingPolicy"] = None,
    manager_locked_cells: Optional[Set[Tuple[str, date]]] = None,
    fairness_weights: Optional["FairnessWeights"] = None,
) -> AutoPilotRunResult:
    """Generate, validate, and summarize a full M/E/N schedule block."""

    from lab_scheduler.scheduling.portage_equity_policy import (
        PortageSchedulingPolicy,
        resolve_portage_scheduling_policy,
    )
    from lab_scheduler.scheduling.shift_cell_locks import fetch_shift_cell_locks
    from lab_scheduler.scheduling.equitability_score import FairnessWeights

    scheduling_policy = portage_scheduling_policy or resolve_portage_scheduling_policy()
    resolved_archetype = normalize_archetype(archetype)
    twelve_hour_mode = resolved_archetype is ScheduleArchetype.TWELVE_HOUR
    assert_monday_block_start(period_start)
    if clear_provisional_state and conn is not None:
        clear_provisional_stretch_state(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=schedule_period_id,
            project_root=project_root,
        )
    aggressor = coverage_aggressor_mode and not bypass_compliance_rules
    resolved_manager_locks = manager_locked_cells
    if resolved_manager_locks is None and conn is not None and tenant_id and schedule_period_id:
        resolved_manager_locks = fetch_shift_cell_locks(
            conn,
            tenant_id=tenant_id,
            schedule_period_id=schedule_period_id,
        )
    try:
        generate = auto_generate_schedule(
            rules=rules,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employees=employees,
            shift_templates=shift_templates,
            shift_required_qualifications=shift_required_qualifications,
            employee_target_hours=employee_target_hours,
            availability_blocked=availability_blocked,
            coverage_targets=coverage_targets,
            require_master_compliance=require_master_compliance and not bypass_compliance_rules,
            coverage_aggressor_mode=aggressor,
            strict_complete_block=strict_complete_block,
            emit_triage=emit_triage,
            archetype=resolved_archetype.value,
            progress_callback=progress_callback,
            enable_fairness_rerun=enable_fairness_rerun,
            portage_scheduling_policy=scheduling_policy,
            manager_locked_cells=resolved_manager_locks,
            fairness_weights=fairness_weights or FairnessWeights(),
        )
    except LogicWorkerFailure as exc:
        raise AutoPilotError(str(exc)) from exc
    except DeterministicScheduleFailure as exc:
        gap_summary = "; ".join(
            f"{gap.assignment_date.isoformat()} {gap.shift_code}"
            for gap in exc.result.clinical_gap_reports[:5]
        )
        detail = f" Gaps: {gap_summary}" if gap_summary else ""
        conflicts = (
            exc.result.compliance_validation.conflicts
            if exc.result.compliance_validation is not None
            else ()
        )
        raise AutoPilotError(
            f"Deterministic-First FAILURE — {exc.message}.{detail}",
            conflicts=conflicts,
            conflict_report_path=exc.result.conflict_report_path,
        ) from exc
    except ImmediateClinicalFailure as exc:
        raise AutoPilotError(str(exc)) from exc
    except ClinicalShortageError as exc:
        raise AutoPilotError(str(exc)) from exc
    if generate.deterministic_status == "FAILURE" and not aggressor and strict_complete_block:
        conflicts = (
            generate.compliance_validation.conflicts
            if generate.compliance_validation is not None
            else ()
        )
        raise AutoPilotError(
            "Deterministic-First schedule did not reach 100% compliance",
            conflicts=conflicts,
            conflict_report_path=generate.conflict_report_path,
        )
    if not bypass_compliance_rules and not aggressor and not twelve_hour_mode:
        try:
            validate_generated_schedule(
                generate,
                rules=rules,
                employees=employees,
                shift_templates=shift_templates,
                period_start=period_start,
                period_end=period_end,
                weeks_in_period=weeks_in_period,
                employee_target_hours=employee_target_hours,
                master_schedule=generate.deterministic_status in ("SUCCESS", "PROVISIONAL"),
            )
        except RuntimeError as exc:
            conflicts = (
                generate.compliance_validation.conflicts
                if generate.compliance_validation is not None
                else ()
            )
            raise AutoPilotError(
                str(exc),
                conflicts=conflicts,
                conflict_report_path=generate.conflict_report_path,
            ) from exc
    proof = build_auto_pilot_proof(
        generate=generate,
        rules=rules,
        employees=employees,
        shift_templates=shift_templates,
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
        employee_target_hours=employee_target_hours,
        twelve_hour_mode=twelve_hour_mode,
    )
    if (
        not bypass_compliance_rules
        and not twelve_hour_mode
    ):
        from lab_scheduler.engine.demand import infer_qual_code
        from lab_scheduler.scheduling.schedule_tallies import shift_band_from_template_code

        template_id_to_band = {
            template_id: shift_band_from_template_code(info.code)
            for template_id, info in shift_templates.items()
        }
        qual_codes = {employee.id: infer_qual_code(employee) for employee in employees}
        gap_reports = getattr(generate, "clinical_gap_reports", ()) or ()
        clinical_gap_messages = tuple(
            f"{gap.assignment_date.isoformat()} {gap.shift_code}: {getattr(gap, 'reason', 'unfilled seat')}"
            for gap in gap_reports
        )
        core_violations = find_core_persist_violations(
            assignments=generate.assignments,
            employees=employees,
            shift_templates=shift_templates,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            rules=rules,
            qual_codes=qual_codes,
            template_id_to_band=template_id_to_band,
            coverage_complete=proof.coverage_complete,
            coverage_gap_count=proof.coverage_gap_count,
            clinical_gap_messages=clinical_gap_messages,
            compliance_first=bool(getattr(generate, "compliance_first", False)),
        )
        generate.core_persist_violations = [violation.to_dict() for violation in core_violations]
        if core_violations:
            log_core_persist_violations(core_violations)
    if (
        not bypass_compliance_rules
        and not aggressor
        and not twelve_hour_mode
        and proof.compliance_error_count > 0
    ):
        raise AutoPilotError(
            "Auto-Pilot generated assignments that failed post-validation compliance checks",
            conflict_report_path=generate.conflict_report_path,
        )
    return AutoPilotRunResult(generate=generate, proof=proof)


def _shift_assignments_has_system_note(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA table_info(shift_assignments)").fetchall()
    return any(str(row[1]) == "system_note" for row in rows)


def dedupe_planned_assignments(
    assignments: Sequence[PlannedAssignment],
    *,
    template_id_to_band: Optional[Mapping[str, str]] = None,
) -> list[PlannedAssignment]:
    """One row per employee per day (matches shift_assignments UNIQUE constraint).

    When ``template_id_to_band`` is supplied, Evening/Night clinical seats win over
    Day assignments for the same employee-day (last-wins alone drops E/N tallies).
    """

    _band_priority = {"N": 2, "E": 3, "D": 1}

    def _band(assignment: PlannedAssignment) -> str:
        if template_id_to_band is None:
            return ""
        return template_id_to_band.get(str(assignment.shift_template_id), "")

    def _priority(assignment: PlannedAssignment) -> int:
        if template_id_to_band is None:
            return -1
        return _band_priority.get(_band(assignment), 0)

    def _prefer_assignment(
        existing: PlannedAssignment,
        candidate: PlannedAssignment,
    ) -> PlannedAssignment:
        existing_frozen = getattr(existing, "master_template_frozen", False)
        candidate_frozen = getattr(candidate, "master_template_frozen", False)
        if candidate_frozen and not existing_frozen:
            return candidate
        if existing_frozen and not candidate_frozen:
            return existing
        existing_band = _band(existing)
        candidate_band = _band(candidate)
        if {existing_band, candidate_band} <= {"E", "N"} and existing_band != candidate_band:
            return existing
        existing_pri = _priority(existing)
        candidate_pri = _priority(candidate)
        if candidate_pri > existing_pri:
            return candidate
        if candidate_pri < existing_pri:
            return existing
        return candidate

    by_key_lists: dict[tuple[str, date], list[PlannedAssignment]] = {}
    for assignment in assignments:
        key = (assignment.employee_id, assignment.assignment_date)
        by_key_lists.setdefault(key, []).append(assignment)

    if template_id_to_band is None:
        return [candidates[-1] for candidates in by_key_lists.values()]

    def _pick_for_band(
        candidates: Sequence[PlannedAssignment],
        band: str,
    ) -> PlannedAssignment:
        matched = [candidate for candidate in candidates if _band(candidate) == band]
        winner = matched[0]
        for candidate in matched[1:]:
            winner = _prefer_assignment(winner, candidate)
        return winner

    pruned_by_key: dict[tuple[str, date], list[PlannedAssignment]] = {}
    for key, candidates in by_key_lists.items():
        bands_present = {_band(candidate) for candidate in candidates}
        if "E" in bands_present or "N" in bands_present:
            pruned_by_key[key] = [
                candidate
                for candidate in candidates
                if _band(candidate) in {"E", "N"}
            ]
        elif "D" in bands_present:
            pruned_by_key[key] = [_pick_for_band(candidates, "D")]
        else:
            pruned_by_key[key] = list(candidates)
    by_key_lists = pruned_by_key

    def _can_band(candidates: Sequence[PlannedAssignment], band: str) -> bool:
        return any(_band(candidate) == band for candidate in candidates)

    chosen: dict[tuple[str, date], PlannedAssignment] = {}
    all_days = sorted({key[1] for key in by_key_lists})

    for day in all_days:
        day_keys = [key for key in by_key_lists if key[1] == day]
        evening_pool = [key for key in day_keys if _can_band(by_key_lists[key], "E")]
        night_pool = [key for key in day_keys if _can_band(by_key_lists[key], "N")]

        best_evening: set[tuple[str, date]] = set()
        best_night: set[tuple[str, date]] = set()
        best_score = (999, 999)

        evening_sizes = (2, 1, 0)
        night_sizes = (2, 1, 0)
        for evening_size in evening_sizes:
            if evening_size > len(evening_pool):
                continue
            evening_options = (
                [()]
                if evening_size == 0
                else list(combinations(evening_pool, evening_size))
            )
            for evening_pick in evening_options:
                evening_set = set(evening_pick)
                night_available = [key for key in night_pool if key not in evening_set]
                for night_size in night_sizes:
                    if night_size > len(night_available):
                        continue
                    night_options = (
                        [()]
                        if night_size == 0
                        else list(combinations(night_available, night_size))
                    )
                    for night_pick in night_options:
                        night_set = set(night_pick)
                        evening_deficit = max(0, 2 - len(evening_set))
                        night_deficit = max(0, 2 - len(night_set))
                        score = (
                            evening_deficit + night_deficit,
                            max(evening_deficit, night_deficit),
                        )
                        if score < best_score:
                            best_score = score
                            best_evening = evening_set
                            best_night = night_set
                        if score == (0, 0):
                            break
                    if best_score == (0, 0):
                        break
                if best_score == (0, 0):
                    break
            if best_score == (0, 0):
                break

        clinical_keys = best_evening | best_night
        for key in best_evening:
            chosen[key] = _pick_for_band(by_key_lists[key], "E")
        for key in best_night:
            chosen[key] = _pick_for_band(by_key_lists[key], "N")

        for key in day_keys:
            if key in clinical_keys:
                continue
            candidates = by_key_lists[key]
            if _can_band(candidates, "D"):
                chosen[key] = _pick_for_band(candidates, "D")
                continue
            winner = candidates[0]
            for candidate in candidates[1:]:
                winner = _prefer_assignment(winner, candidate)
            chosen[key] = winner

        for needed_band, other_band in (("N", "E"), ("E", "N")):
            while True:
                evening_count = sum(
                    1 for key in day_keys if _band(chosen[key]) == "E"
                )
                night_count = sum(
                    1 for key in day_keys if _band(chosen[key]) == "N"
                )
                current = evening_count if needed_band == "E" else night_count
                other = night_count if needed_band == "E" else evening_count
                if current >= 2:
                    break
                swapped = False
                for key in day_keys:
                    if (
                        _band(chosen[key]) == other_band
                        and _can_band(by_key_lists[key], needed_band)
                        and other > current
                    ):
                        chosen[key] = _pick_for_band(by_key_lists[key], needed_band)
                        swapped = True
                        break
                if swapped:
                    continue
                for key in day_keys:
                    if _band(chosen[key]) == needed_band:
                        continue
                    if _can_band(by_key_lists[key], needed_band):
                        chosen[key] = _pick_for_band(by_key_lists[key], needed_band)
                        swapped = True
                        break
                if not swapped:
                    break

    deduped = list(chosen.values())

    return deduped


_dedupe_planned_assignments = dedupe_planned_assignments


def _clear_assignments_for_period_window(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
) -> None:
    """Remove all assignment rows in the period calendar window for this tenant."""

    try:
        period_row = conn.execute(
            """
            SELECT period_start, period_end_inclusive
            FROM schedule_periods
            WHERE tenant_id = ? AND id = ?
            """,
            (tenant_id, schedule_period_id),
        ).fetchone()
    except sqlite3.OperationalError:
        period_row = None
    if period_row is None:
        conn.execute(
            """
            DELETE FROM shift_assignments
            WHERE tenant_id = ? AND schedule_period_id = ?
            """,
            (tenant_id, schedule_period_id),
        )
        return
    conn.execute(
        """
        DELETE FROM shift_assignments
        WHERE tenant_id = ?
          AND assignment_date >= ?
          AND assignment_date <= ?
        """,
        (tenant_id, period_row[0], period_row[1]),
    )


def persist_auto_pilot_schedule(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    schedule_period_id: str,
    assignments: Sequence[PlannedAssignment],
    replace_existing: bool = True,
    provisional_assignments: Optional[Sequence[ProvisionalAssignment]] = None,
) -> int:
    """Persist generated assignments in one atomic batch transaction."""

    assignments = dedupe_planned_assignments(assignments)
    conn.execute("PRAGMA foreign_keys = ON;")
    now = _utc_now_iso()
    has_system_note = _shift_assignments_has_system_note(conn)
    provisional_keys = {
        item.assignment_key()
        for item in (provisional_assignments or ())
    }

    def _system_note_for(assignment: PlannedAssignment) -> Optional[str]:
        key = (assignment.employee_id, assignment.assignment_date, assignment.shift_template_id)
        if assignment.approved_stretch:
            return approved_stretch_system_note()
        if assignment.contract_line_exception:
            return contract_line_exception_system_note(
                violation_message=assignment.contract_line_exception_message,
            )
        if assignment.provisional_compliance or key in provisional_keys:
            return provisional_stretch_system_note()
        if assignment.forced_clinical_ot:
            return "FORCED_CLINICAL_OT"
        return None

    if has_system_note:
        rows = [
            (
                f"asg-{uuid.uuid4().hex[:12]}",
                tenant_id,
                schedule_period_id,
                assignment.employee_id,
                assignment.shift_template_id,
                assignment.assignment_date.isoformat(),
                now,
                now,
                _system_note_for(assignment),
            )
            for assignment in assignments
        ]
    else:
        rows = [
            (
                f"asg-{uuid.uuid4().hex[:12]}",
                tenant_id,
                schedule_period_id,
                assignment.employee_id,
                assignment.shift_template_id,
                assignment.assignment_date.isoformat(),
                now,
                now,
            )
            for assignment in assignments
        ]

    conn.execute("BEGIN IMMEDIATE")
    try:
        if replace_existing:
            _clear_assignments_for_period_window(
                conn,
                tenant_id=tenant_id,
                schedule_period_id=schedule_period_id,
            )
        if rows:
            if has_system_note:
                conn.executemany(
                    """
                    INSERT INTO shift_assignments (
                      id, tenant_id, schedule_period_id, employee_id,
                      shift_template_id, assignment_date, created_at, updated_at, system_note
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            else:
                conn.executemany(
                    """
                    INSERT INTO shift_assignments (
                      id, tenant_id, schedule_period_id, employee_id,
                      shift_template_id, assignment_date, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(rows)
