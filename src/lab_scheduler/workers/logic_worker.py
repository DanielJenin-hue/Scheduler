from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Callable, Mapping, Optional, Protocol, Sequence, TypeVar

from lab_scheduler.errors.schedule_error import ScheduleError
from lab_scheduler.time import workweek_for

T = TypeVar("T")

_LINE_NUMBER_PATTERN = re.compile(r"Line\s+(\d+)", re.IGNORECASE)


class LogicWorkerStatus(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class TriageEntry:
    """Structured triage row for orchestrator / auditor handoff."""

    slot_id: str
    slot: str
    assignment_date: date
    error_code: ScheduleError
    blocked_by: ScheduleError
    deficit_hours: float
    shift_code: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "slot_id": self.slot_id,
            "slot": self.slot,
            "date": self.assignment_date.isoformat(),
            "error_code": self.error_code.value,
            "failed_rule_code": self.error_code.value,
            "blocked_by": self.blocked_by.value,
            "deficit_hours": round(float(self.deficit_hours), 2),
        }
        if self.shift_code is not None:
            payload["shift_code"] = self.shift_code
        return payload


@dataclass(frozen=True, slots=True)
class LogicWorkerOutput:
    status: LogicWorkerStatus
    assignments: tuple[dict[str, object], ...]
    triage_list: tuple[TriageEntry, ...] = ()
    shift_equity_metrics: Mapping[str, object] = field(default_factory=dict)
    staff_fairness_report: Mapping[str, object] = field(default_factory=dict)
    schedule_archetype: str = "STANDARD"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "assignments": list(self.assignments),
            "triage_list": [entry.to_dict() for entry in self.triage_list],
            "shift_equity_metrics": dict(self.shift_equity_metrics),
            "staff_fairness_report": dict(self.staff_fairness_report),
            "schedule_archetype": self.schedule_archetype,
        }


class LogicWorkerFailure(Exception):
    """Hard failure for preflight rejection or strict export with zero assignments."""

    def __init__(
        self,
        *,
        error: ScheduleError,
        message: str,
        assignment_date: Optional[date] = None,
        shift_code: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.error = error
        self.assignment_date = assignment_date
        self.shift_code = shift_code

    def to_dict(self) -> dict[str, str]:
        payload: dict[str, str] = {
            "status": LogicWorkerStatus.FAILURE.value,
            "error_code": self.error.value,
            "message": str(self),
        }
        if self.assignment_date is not None:
            payload["assignment_date"] = self.assignment_date.isoformat()
        if self.shift_code is not None:
            payload["shift_code"] = self.shift_code
        return payload


class LogicWorkerRejection(LogicWorkerFailure):
    """Preflight rejection before any database load or generation cycle."""

    def __init__(
        self,
        *,
        error: ScheduleError,
        message: str,
        period_start: date,
        expected_monday: date,
    ) -> None:
        super().__init__(error=error, message=message, assignment_date=period_start)
        self.period_start = period_start
        self.expected_monday = expected_monday

    def to_dict(self) -> dict[str, str]:
        payload = super().to_dict()
        payload["period_start"] = self.period_start.isoformat()
        payload["expected_monday"] = self.expected_monday.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class LogicWorkerPayload:
    """Minimal inbound payload for logic-worker preflight gates."""

    period_start: date
    period_end: Optional[date] = None
    tenant_id: Optional[str] = None
    schedule_period_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class LogicWorkerAcceptance:
    """Payload passed all entry-point gates and may proceed to generation."""

    block_start_monday: date
    period_start: date
    period_end: Optional[date] = None
    tenant_id: Optional[str] = None
    schedule_period_id: Optional[str] = None


@dataclass
class GenerationTriageSink:
    """Mutable collector populated by ``auto_generate_schedule(emit_triage=True)``."""

    entries: list[TriageEntry] = field(default_factory=list)


class LogicWorkerGenerationResult(Protocol):
    assignments: Sequence[object]
    triage_list: Sequence[TriageEntry]
    schedule_archetype: str


def schedule_error_for_unfillable_slot(
    *,
    slot_is_impossible: bool,
    qualified_staff_exist: bool,
) -> ScheduleError:
    if slot_is_impossible or not qualified_staff_exist:
        return ScheduleError.ERR_IMPOSSIBLE_COVERAGE
    return ScheduleError.LABOR_RULE


def infer_blocked_by_rule(
    *,
    slot_is_impossible: bool,
    qualified_staff_exist: bool,
    constraint_summary: Optional[str] = None,
    ineligible_reasons: Optional[Mapping[str, str]] = None,
) -> ScheduleError:
    if slot_is_impossible or not qualified_staff_exist:
        return ScheduleError.ERR_IMPOSSIBLE_COVERAGE

    haystack = " ".join(
        part
        for part in (
            constraint_summary or "",
            " ".join((ineligible_reasons or {}).values()),
        )
        if part
    ).lower()

    if "160" in haystack or "contract" in haystack or "fte" in haystack:
        return ScheduleError.CONTRACT_FTE_160
    if "weekly" in haystack and ("hour" in haystack or "40" in haystack):
        return ScheduleError.MAX_WEEKLY_HOURS
    if "turnaround" in haystack or "15h" in haystack:
        return ScheduleError.UNION_TURNAROUND_15H
    if "consecutive" in haystack:
        return ScheduleError.CONSECUTIVE_DAYS
    if "11h" in haystack or "morning rest" in haystack:
        return ScheduleError.UNION_MORNING_REST_11H
    if "fatigue" in haystack or "6 day" in haystack or "six day" in haystack:
        return ScheduleError.PORTAGE_CONSECUTIVE_DAYS
    return ScheduleError.LABOR_RULE


def build_slot_id(
    *,
    assignment_date: date,
    shift_id: str,
    shift_code: str,
    role_pool_id: str,
    seat_index: int,
    required_qual_code: Optional[str],
) -> str:
    qual = required_qual_code or "ANY"
    return (
        f"{assignment_date.isoformat()}|{shift_code}|{shift_id}|"
        f"{role_pool_id}|seat={seat_index}|qual={qual}"
    )


def format_vacant_slot_label(
    *,
    role_pool_id: str,
    required_qual_code: Optional[str],
    seat_index: int,
    shift_code: str,
) -> str:
    qual = required_qual_code or "MLT"
    line_match = _LINE_NUMBER_PATTERN.search(role_pool_id)
    line_num = int(line_match.group(1)) if line_match else seat_index + 1
    contract = "D/N" if shift_code == "NIGHT" else "D/E"
    if "D/N" in role_pool_id.upper():
        contract = "D/N"
    return f"Vacant {qual} {contract} - Line {line_num:02d}"


def append_triage_entry(
    sink: GenerationTriageSink,
    *,
    assignment_date: date,
    shift_code: str,
    shift_id: str,
    role_pool_id: str,
    seat_index: int,
    required_qual_code: Optional[str],
    shift_hours: float,
    slot_is_impossible: bool,
    qualified_staff_exist: bool,
    constraint_summary: Optional[str] = None,
    ineligible_reasons: Optional[Mapping[str, str]] = None,
) -> TriageEntry:
    error_code = schedule_error_for_unfillable_slot(
        slot_is_impossible=slot_is_impossible,
        qualified_staff_exist=qualified_staff_exist,
    )
    blocked_by = infer_blocked_by_rule(
        slot_is_impossible=slot_is_impossible,
        qualified_staff_exist=qualified_staff_exist,
        constraint_summary=constraint_summary,
        ineligible_reasons=ineligible_reasons,
    )
    entry = TriageEntry(
        slot_id=build_slot_id(
            assignment_date=assignment_date,
            shift_id=shift_id,
            shift_code=shift_code,
            role_pool_id=role_pool_id,
            seat_index=seat_index,
            required_qual_code=required_qual_code,
        ),
        slot=format_vacant_slot_label(
            role_pool_id=role_pool_id,
            required_qual_code=required_qual_code,
            seat_index=seat_index,
            shift_code=shift_code,
        ),
        assignment_date=assignment_date,
        error_code=error_code,
        blocked_by=blocked_by,
        deficit_hours=shift_hours,
        shift_code=shift_code,
    )
    sink.entries.append(entry)
    return entry


def raise_unfillable_slot_failure(
    *,
    assignment_date: date,
    shift_code: str,
    slot_is_impossible: bool,
    qualified_staff_exist: bool,
    constraint_summary: Optional[str] = None,
) -> None:
    error = schedule_error_for_unfillable_slot(
        slot_is_impossible=slot_is_impossible,
        qualified_staff_exist=qualified_staff_exist,
    )
    if error is ScheduleError.ERR_IMPOSSIBLE_COVERAGE:
        detail = (
            "seat cannot be filled within staffing capacity"
            if slot_is_impossible
            else "no qualified employees for shift seat"
        )
    else:
        detail = constraint_summary or "no legal assignment within labor rules"
    raise LogicWorkerFailure(
        error=error,
        message=f"{assignment_date.isoformat()} {shift_code}: {detail}",
        assignment_date=assignment_date,
        shift_code=shift_code,
    )


def handle_unfillable_slot(
    *,
    triage_sink: Optional[GenerationTriageSink],
    strict_raise: bool,
    assignment_date: date,
    shift_code: str,
    shift_id: str,
    role_pool_id: str,
    seat_index: int,
    required_qual_code: Optional[str],
    shift_hours: float,
    slot_is_impossible: bool,
    qualified_staff_exist: bool,
    constraint_summary: Optional[str] = None,
    ineligible_reasons: Optional[Mapping[str, str]] = None,
) -> Optional[TriageEntry]:
    if triage_sink is not None:
        return append_triage_entry(
            triage_sink,
            assignment_date=assignment_date,
            shift_code=shift_code,
            shift_id=shift_id,
            role_pool_id=role_pool_id,
            seat_index=seat_index,
            required_qual_code=required_qual_code,
            shift_hours=shift_hours,
            slot_is_impossible=slot_is_impossible,
            qualified_staff_exist=qualified_staff_exist,
            constraint_summary=constraint_summary,
            ineligible_reasons=ineligible_reasons,
        )
    if strict_raise:
        raise_unfillable_slot_failure(
            assignment_date=assignment_date,
            shift_code=shift_code,
            slot_is_impossible=slot_is_impossible,
            qualified_staff_exist=qualified_staff_exist,
            constraint_summary=constraint_summary,
        )
    return None


def require_monday_block_start(period_start: date) -> date:
    """
    Fail fast when ``period_start`` is not a Monday-start schedule block.

    Uses only calendar math — no database access or generation side effects.
    """

    block_start = workweek_for(period_start).start
    if block_start != period_start:
        raise LogicWorkerRejection(
            error=ScheduleError.ERR_NON_MONDAY_BLOCK_START,
            message=(
                f"Schedule block requires a Monday start; got {period_start.isoformat()} "
                f"(expected {block_start.isoformat()})."
            ),
            period_start=period_start,
            expected_monday=block_start,
        )
    return block_start


def assignment_to_dict(assignment: object) -> dict[str, object]:
    return {
        "employee_id": getattr(assignment, "employee_id"),
        "shift_template_id": getattr(assignment, "shift_template_id"),
        "assignment_date": getattr(assignment, "assignment_date").isoformat(),
    }


def resolve_logic_worker_status(
    *,
    assignments: Sequence[object],
    triage_list: Sequence[TriageEntry],
) -> LogicWorkerStatus:
    if triage_list and assignments:
        return LogicWorkerStatus.PARTIAL_SUCCESS
    if triage_list and not assignments:
        return LogicWorkerStatus.FAILURE
    return LogicWorkerStatus.SUCCESS


def build_logic_worker_output(result: LogicWorkerGenerationResult) -> LogicWorkerOutput:
    triage = tuple(getattr(result, "triage_list", ()) or ())
    assignments = tuple(assignment_to_dict(item) for item in result.assignments)
    status = resolve_logic_worker_status(assignments=result.assignments, triage_list=triage)
    shift_equity_metrics = dict(getattr(result, "shift_equity_metrics", {}) or {})
    schedule_archetype = str(getattr(result, "schedule_archetype", "STANDARD") or "STANDARD")
    return LogicWorkerOutput(
        status=status,
        assignments=assignments,
        triage_list=triage,
        shift_equity_metrics=shift_equity_metrics,
        schedule_archetype=schedule_archetype,
    )


def require_complete_assignment_block(result: LogicWorkerGenerationResult) -> LogicWorkerOutput:
    """
    Build the logic-worker JSON contract from a generation result.

    Preflight rejections still raise ``LogicWorkerRejection``. When triage rows exist
    alongside valid assignments the status is ``PARTIAL_SUCCESS``.
    """

    output = build_logic_worker_output(result)
    if output.status is LogicWorkerStatus.FAILURE and not output.assignments:
        first = output.triage_list[0] if output.triage_list else None
        raise LogicWorkerFailure(
            error=first.error_code if first else ScheduleError.COVERAGE_TARGET,
            message="Generation produced no assignments; all open seats require triage.",
            assignment_date=first.assignment_date if first else None,
            shift_code=first.shift_code if first else None,
        )
    return output


def run_logic_worker(
    payload: LogicWorkerPayload,
    *,
    load_context: Optional[Callable[[LogicWorkerPayload], T]] = None,
    generate: Optional[Callable[[LogicWorkerAcceptance], LogicWorkerGenerationResult]] = None,
) -> LogicWorkerAcceptance | LogicWorkerOutput | T:
    """
    Logic worker entry point.

    Preflight gates run before any optional context loader or generation cycle.
    When ``generate`` is supplied, returns a strict JSON-serializable
    ``LogicWorkerOutput`` with ``status``, ``assignments``, and ``triage_list``.
    """

    block_start = require_monday_block_start(payload.period_start)
    acceptance = LogicWorkerAcceptance(
        block_start_monday=block_start,
        period_start=payload.period_start,
        period_end=payload.period_end,
        tenant_id=payload.tenant_id,
        schedule_period_id=payload.schedule_period_id,
    )
    if generate is not None:
        generated = generate(acceptance)
        return require_complete_assignment_block(generated)
    if load_context is None:
        return acceptance
    return load_context(payload)
