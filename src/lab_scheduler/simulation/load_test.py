from __future__ import annotations



import time

import traceback

from dataclasses import dataclass

from datetime import date

from typing import Dict, List, Optional, Set



from lab_scheduler.compliance import MANITOBA, ShiftTemplateInfo

from lab_scheduler.engine.constraints import (

    compute_coverage_success_rate_pct,

    portage_coverage_targets,

    portage_employee_target_hours,

)

from lab_scheduler.scheduling.auto_generate import DeterministicScheduleFailure
from lab_scheduler.scheduling.auto_pilot import AutoPilotError, run_auto_pilot_full_block
from lab_scheduler.workers.logic_worker import LogicWorkerFailure

from lab_scheduler.scheduling.models import UnfilledSlot

from lab_scheduler.simulation.hospital_stress import (

    PERIOD_END,

    PERIOD_START,

    QUAL_MLA,

    QUAL_MLT,

    shift_required_qualifications,

    shift_templates,

)

from lab_scheduler.simulation.portage_blueprint import (

    PORTAGE_MLA_LINE_COUNT,

    PORTAGE_MLT_LINE_COUNT,

    PORTAGE_ROSTER_SIZE,

    build_portage_blueprint_roster,

)

PORTAGE_MLT_COUNT = PORTAGE_MLT_LINE_COUNT
PORTAGE_MLA_COUNT = PORTAGE_MLA_LINE_COUNT



SIM_TENANT_ID = "tenant-portage-load-test"

WEEKS_IN_PERIOD = 4





def build_portage_roster() -> List:

    """Canonical 25-line Portage blueprint roster (13 MLT + 12 MLA)."""



    return build_portage_blueprint_roster()





@dataclass(frozen=True, slots=True)

class GapAnalysisRow:

    assignment_date: date

    shift_code: str

    reason: str

    is_constraint_violation: bool

    constraint_summary: Optional[str]

    violation_kind: Optional[str] = None

    is_impossible_coverage: bool = False





@dataclass(frozen=True, slots=True)

class CoverageTierGapRow:

    label: str

    target_fte: float

    actual_fte: float

    gap_fte: float

    is_impossible: bool





@dataclass(frozen=True, slots=True)

class LoadTestSummary:

    execution_seconds: float

    roster_size: int

    mlt_count: int

    mla_count: int

    slots_total: int

    compliant_shifts_generated: int

    gap_count: int

    constraint_violation_count: int

    coverage_gap_count: int

    coverage_success_rate_pct: float

    coverage_complete: bool

    compliance_pass_rate_pct: float

    under_five_seconds: bool

    gaps: tuple[GapAnalysisRow, ...]

    tier_gaps: tuple[CoverageTierGapRow, ...]

    exception_occurred: bool

    exception_message: str



    @property

    def fill_rate_pct(self) -> float:

        if self.slots_total == 0:

            return 100.0

        return round(100.0 * self.compliant_shifts_generated / self.slots_total, 2)



    @property

    def passed(self) -> bool:

        return (

            not self.exception_occurred

            and self.compliance_pass_rate_pct >= 100.0

            and self.coverage_success_rate_pct >= 85.0

            and self.under_five_seconds

        )





def _gap_rows(unfilled: List[UnfilledSlot]) -> tuple[GapAnalysisRow, ...]:

    return tuple(

        GapAnalysisRow(

            assignment_date=slot.assignment_date,

            shift_code=slot.shift_code,

            reason=slot.reason,

            is_constraint_violation=slot.is_constraint_violation,

            constraint_summary=slot.constraint_summary,

            violation_kind=slot.violation_kind,

            is_impossible_coverage=slot.is_impossible_coverage,

        )

        for slot in unfilled

    )





def run_portage_load_test(

    *,

    period_start: date = PERIOD_START,

    period_end: date = PERIOD_END,

    weeks_in_period: int = WEEKS_IN_PERIOD,

) -> LoadTestSummary:

    """

    Batch Auto-Pilot generate for a Portage-scale roster over a 4-week block.



    Returns a Load Test Summary suitable for CLI output and the Stress-Test UI.

    """



    exception_occurred = False

    exception_message = ""

    execution_seconds = 0.0

    slots_total = 0

    compliant_shifts_generated = 0

    gap_count = 0

    constraint_violation_count = 0

    coverage_gap_count = 0

    coverage_success_rate_pct = 0.0

    coverage_complete = False

    compliance_pass_rate_pct = 0.0

    gaps: tuple[GapAnalysisRow, ...] = ()

    tier_gaps: tuple[CoverageTierGapRow, ...] = ()



    t0 = time.perf_counter()

    try:

        employees = build_portage_roster()

        templates = shift_templates()

        shift_quals = shift_required_qualifications()

        coverage_targets = portage_coverage_targets(employees)

        target_hours = portage_employee_target_hours(

            employees,

            weeks_in_period=weeks_in_period,

            rules=MANITOBA,

        )



        pilot = run_auto_pilot_full_block(
            rules=MANITOBA,
            period_start=period_start,
            period_end=period_end,
            weeks_in_period=weeks_in_period,
            employees=employees,
            shift_templates=templates,
            shift_required_qualifications=shift_quals,
            employee_target_hours=target_hours,
            coverage_targets=coverage_targets,
            coverage_aggressor_mode=True,
            strict_complete_block=False,
        )



        slots_total = pilot.generate.slots_total

        compliant_shifts_generated = pilot.generate.slots_filled

        gap_count = len(pilot.generate.unfilled)

        constraint_violation_count = sum(

            1

            for slot in pilot.generate.unfilled

            if slot.violation_kind == "LABOR_RULE" or slot.is_constraint_violation

        )

        coverage_gap_count = pilot.generate.coverage_gap_count

        coverage_complete = pilot.proof.coverage_complete

        coverage_success_rate_pct = pilot.proof.coverage_success_rate_pct

        gaps = _gap_rows(pilot.generate.unfilled)

        tier_gaps = tuple(

            CoverageTierGapRow(

                label=result.label,

                target_fte=result.target_fte,

                actual_fte=result.actual_fte,

                gap_fte=result.gap_fte,

                is_impossible=result.is_impossible,

            )

            for result in pilot.generate.coverage_tier_results

            if (not result.meets_target or result.gap_fte >= 0.05)
            and not result.is_impossible

        )



        if pilot.proof.compliance_error_count == 0:

            compliance_pass_rate_pct = 100.0

        else:

            compliance_pass_rate_pct = round(pilot.proof.legal_compliance_pct, 2)

    except (
        AutoPilotError,
        LogicWorkerFailure,
        RuntimeError,
        DeterministicScheduleFailure,
    ) as exc:

        exception_occurred = True

        exception_message = str(exc)

    except Exception as exc:

        exception_occurred = True

        exception_message = f"{type(exc).__name__}: {exc}"

        traceback.print_exc()

    finally:

        execution_seconds = round(time.perf_counter() - t0, 3)



    return LoadTestSummary(

        execution_seconds=execution_seconds,

        roster_size=PORTAGE_ROSTER_SIZE,

        mlt_count=PORTAGE_MLT_LINE_COUNT,

        mla_count=PORTAGE_MLA_LINE_COUNT,

        slots_total=slots_total,

        compliant_shifts_generated=compliant_shifts_generated,

        gap_count=gap_count,

        constraint_violation_count=constraint_violation_count,

        coverage_gap_count=coverage_gap_count,

        coverage_success_rate_pct=coverage_success_rate_pct,

        coverage_complete=coverage_complete,

        compliance_pass_rate_pct=compliance_pass_rate_pct,

        under_five_seconds=execution_seconds < 5.0,

        gaps=gaps,

        tier_gaps=tier_gaps,

        exception_occurred=exception_occurred,

        exception_message=exception_message,

    )





def format_load_test_summary(summary: LoadTestSummary) -> str:

    lines = [

        "=" * 60,

        "  PORTAGE-SCALE LOAD TEST SUMMARY",

        "=" * 60,

        f"  Roster               : {summary.roster_size} staff "

        f"({summary.mlt_count} MLT / {summary.mla_count} MLA)",

        f"  Period               : {PERIOD_START} -> {PERIOD_END}",

        "-" * 60,

        f"  Execution time       : {summary.execution_seconds:>8.3f} s"

        f" {'(PASS <5s)' if summary.under_five_seconds else '(FAIL >=5s)'}",

        f"  Compliant shifts     : {summary.compliant_shifts_generated}",

        f"  Slots total          : {summary.slots_total}",

        f"  Fill rate            : {summary.fill_rate_pct:>8.2f} %",

        f"  Gaps (unfilled)      : {summary.gap_count}",

        f"  Constraint violations: {summary.constraint_violation_count}",

        f"  Coverage gaps         : {summary.coverage_gap_count}",

        f"  Coverage success rate : {summary.coverage_success_rate_pct:>8.2f} %"

        f" {'(PASS >=85%)' if summary.coverage_success_rate_pct >= 85.0 else '(FAIL)'}",

        f"  Coverage complete     : {'YES' if summary.coverage_complete else 'NO'}",

        f"  Compliance pass rate : {summary.compliance_pass_rate_pct:>8.2f} %"

        f" {'(PASS 100%)' if summary.compliance_pass_rate_pct >= 100.0 else '(FAIL)'}",

        "-" * 60,

    ]

    if summary.exception_occurred:

        lines.append(f"  Status               : FAILED - {summary.exception_message}")

    elif summary.passed:

        lines.append("  Status               : PASSED")

    else:

        lines.append("  Status               : COMPLETED WITH WARNINGS")

    if summary.gaps:

        lines.append("-" * 60)

        lines.append("  Gap analysis (first 10):")

        for gap in summary.gaps[:10]:

            if gap.is_impossible_coverage:

                flag = "IMPOSSIBLE"

            elif gap.violation_kind == "LABOR_RULE" or gap.is_constraint_violation:

                flag = "LABOR_RULE"

            elif gap.violation_kind == "COVERAGE_TARGET":

                flag = "COVERAGE"

            else:

                flag = "STAFFING"

            lines.append(

                f"    [{flag}] {gap.assignment_date.isoformat()} · "

                f"{gap.shift_code} — {gap.reason}"

            )

        if len(summary.gaps) > 10:

            lines.append(f"    ... and {len(summary.gaps) - 10} more gap(s)")

    if summary.tier_gaps:

        lines.append("-" * 60)

        lines.append("  Coverage tier gaps (first 10):")

        for tier in summary.tier_gaps[:10]:

            impossible = " IMPOSSIBLE" if tier.is_impossible else ""

            lines.append(

                f"    {tier.label}: Target {tier.target_fte:g}, "

                f"Actual {tier.actual_fte:g}{impossible}"

            )

    lines.append("=" * 60)

    return "\n".join(lines)


