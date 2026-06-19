"""Shadow-test harness for the ROUTER-8H LLM scheduling engine.

Wires the new flat-availability export to the legacy ComplianceValidator so a
ROUTER-8H response can be graded end-to-end. The pipeline has three seams:

    1. export_period()          -> flat_availability.v1 payload  (the LLM INPUT)
    2. route(payload)           -> {employee_id: {date: shift_code}}  (the LLM OUTPUT)
    3. ingest_and_grade(...)    -> ScheduledShifts + gap delta + ComplianceValidationResult

Step 2 is the only seam that talks to a model. In production you replace
`reference_route()` with a real API call (Claude / GPT) that is handed the
ROUTER-8H system prompt + the payload JSON. There is NO live model in this
sandbox, so the default adapter stands in by running the legacy production
scheduler and reshaping its output into the ROUTER-8H schema. This lets us prove
the receiver, the Demand-minus-Assigned gap delta, and the umpire all fire on a
real, messy (PTO/sick-call) period.

Run:  python scripts/shadow_test_router_8h.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Mapping, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from portage_fixtures import portage_generate_kwargs  # noqa: E402

from lab_scheduler.audit.compliance import ComplianceValidationResult, ComplianceValidator  # noqa: E402
from lab_scheduler.compliance.engine import ScheduledShift, ShiftTemplateInfo  # noqa: E402
from lab_scheduler.engine.demand import CLINICAL_FLOOR  # noqa: E402
from lab_scheduler.finance.penalty_score import (  # noqa: E402
    PenaltyBreakdown,
    gainshare_delta,
    score_schedule,
)
from lab_scheduler.prompts import ROUTER_8H_SYSTEM_PROMPT  # noqa: E402
from lab_scheduler.scheduling.auto_generate import auto_generate_schedule  # noqa: E402
from lab_scheduler.scheduling.flat_availability import build_llm_constraint_payload  # noqa: E402
from lab_scheduler.scheduling.profiles import EmployeeProfile  # noqa: E402
from lab_scheduler.scheduling.strategies import ScheduleArchetype  # noqa: E402

VALID_8H_CODES: Set[str] = {"MORNING", "EVENING", "NIGHT"}
ARTIFACT_DIR = ROOT / "artifacts" / "shadow_router_8h"


def build_availability_blocks(
    employees: Sequence[EmployeeProfile],
    dates: Sequence[date],
) -> Dict[str, Set[date]]:
    """Synthesize a 'messy' period: two vacation blocks + scattered sick calls.

    Targets are derived from the live roster (by qualification) rather than
    hardcoded ids, so this works regardless of the roster's id convention.
    """
    blocked: Dict[str, Set[date]] = {}
    mlt = [e.id for e in employees if "qual-mlt" in (e.qualification_ids or set())]
    mla = [e.id for e in employees if "qual-mla" in (e.qualification_ids or set())]

    def block(emp_id: str | None, days: Sequence[date]) -> None:
        if emp_id and days:
            blocked.setdefault(emp_id, set()).update(days)

    # Two full-week vacation blocks early in the period.
    if mlt:
        block(mlt[0], dates[0:7])
    if mla:
        block(mla[0], dates[14:21])
    # Scattered single-day sick calls across the pool.
    sick_targets = [grp[i] for grp, i in ((mlt, 2), (mla, 3), (mlt, 4), (mla, 5)) if len(grp) > i]
    for idx, emp_id in enumerate(sick_targets):
        day_idx = 3 + idx * 9
        block(emp_id, [dates[day_idx if day_idx < len(dates) else -1]])
    return blocked


def export_period(
    *,
    period_start: date,
    period_end: date,
    weeks_in_period: int,
) -> Tuple[dict, dict]:
    """Stage 1: build the flat_availability payload (the LLM input).

    Returns ``(payload, context)`` where context carries the live objects the
    grader needs (employees, templates, rules, target hours, blocks).
    """
    kwargs = portage_generate_kwargs(
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
    )
    employees: List[EmployeeProfile] = kwargs["employees"]
    templates: Mapping[str, ShiftTemplateInfo] = kwargs["shift_templates"]
    target_hours: Mapping[str, float] = kwargs["employee_target_hours"]
    dates = [period_start + timedelta(days=i) for i in range((period_end - period_start).days + 1)]

    blocks = build_availability_blocks(employees, dates)
    daily_demand = {day: dict(CLINICAL_FLOOR) for day in dates}

    payload = build_llm_constraint_payload(
        employees=employees,
        dates=dates,
        shift_templates=templates,
        assignments=(),  # greenfield: the router fills from scratch
        availability_blocked=blocks,
        target_hours=target_hours,
        daily_demand=daily_demand,
    )
    context = {
        "kwargs": kwargs,
        "employees": employees,
        "templates": templates,
        "target_hours": target_hours,
        "dates": dates,
        "blocks": blocks,
        "daily_demand": daily_demand,
    }
    return payload, context


def reference_route(payload: dict, context: dict) -> Dict[str, Dict[str, str]]:
    """Stage 2 (STAND-IN): produce a ROUTER-8H-shaped schedule.

    >>> PRODUCTION: replace this body with an LLM API call that sends the
    >>> ROUTER-8H system prompt + json.dumps(payload) and json.loads the reply.

    Here we run the legacy production scheduler and reshape its assignments into
    {employee_id: {date_iso: shift_code}} so the rest of the pipeline is exercised.
    """
    kwargs = dict(context["kwargs"])
    result = auto_generate_schedule(
        **kwargs,
        archetype=ScheduleArchetype.STANDARD.value,
        availability_blocked=context["blocks"],
    )
    templates = context["templates"]
    routed: Dict[str, Dict[str, str]] = {}
    for assignment in result.assignments:
        template = templates.get(assignment.shift_template_id)
        if template is None or template.code not in VALID_8H_CODES:
            continue
        routed.setdefault(assignment.employee_id, {})[assignment.assignment_date.isoformat()] = template.code
    return routed


def baseline_route(payload: dict, context: dict) -> Dict[str, Dict[str, str]]:
    """A deliberately unoptimized 'last year' baseline: concentrate every band on
    the same handful of staff so hours pile far over target (overtime), and let
    blocked days fall through as gaps. This is the schedule the gainshare model
    compares the agent against.
    """
    employees = context["employees"]
    dates = context["dates"]
    blocks = context["blocks"]
    daily_demand = context["daily_demand"]
    mlt = [e.id for e in employees if "qual-mlt" in (e.qualification_ids or set())]
    mla = [e.id for e in employees if "qual-mla" in (e.qualification_ids or set())]

    def pair(idx: int) -> List[str]:
        return [p[idx] for p in (mlt, mla) if len(p) > idx]

    band_pools = {"MORNING": pair(0), "EVENING": pair(1), "NIGHT": pair(2)}
    routed: Dict[str, Dict[str, str]] = {}
    for day in dates:
        for code in daily_demand[day]:
            for emp_id in band_pools.get(code, []):
                if day in blocks.get(emp_id, set()):
                    continue  # blocked -> leaves a gap, as a bad schedule would
                routed.setdefault(emp_id, {})[day.isoformat()] = code
    return routed


def ingest_router_response(
    routed: Mapping[str, Mapping[str, str]],
    context: dict,
) -> List[ScheduledShift]:
    """Stage 3a (RECEIVER): parse the flat LLM map into ScheduledShift records."""
    templates = context["templates"]
    code_to_template = {t.code: tid for tid, t in templates.items()}
    name_by_id = {e.id: e.full_name for e in context["employees"]}

    shifts: List[ScheduledShift] = []
    for employee_id, day_map in routed.items():
        for day_iso, shift_code in day_map.items():
            template_id = code_to_template.get(shift_code)
            if template_id is None:
                continue  # router emitted an unknown band; drop (would also be a gap)
            shifts.append(
                ScheduledShift(
                    employee_id=employee_id,
                    employee_name=name_by_id.get(employee_id, employee_id),
                    assignment_date=date.fromisoformat(day_iso),
                    shift_template_id=template_id,
                )
            )
    return shifts


def compute_gap_delta(
    routed: Mapping[str, Mapping[str, str]],
    context: dict,
) -> Tuple[int, List[dict]]:
    """Stage 3b (DELTA): Demand minus Assigned. The LLM never self-reports gaps."""
    assigned = Counter()
    for day_map in routed.values():
        for day_iso, shift_code in day_map.items():
            assigned[(day_iso, shift_code)] += 1

    gaps: List[dict] = []
    total = 0
    for day, demand in sorted(context["daily_demand"].items()):
        for shift_code, required in demand.items():
            have = assigned[(day.isoformat(), shift_code)]
            short = max(0, required - have)
            if short:
                total += short
                gaps.append(
                    {
                        "date": day.isoformat(),
                        "shift_code": shift_code,
                        "required": required,
                        "assigned": have,
                        "unfilled": short,
                    }
                )
    return total, gaps


def run_umpire(shifts: Sequence[ScheduledShift], context: dict) -> ComplianceValidationResult:
    """Stage 4 (UMPIRE): the legacy ComplianceValidator grades the router output."""
    kwargs = context["kwargs"]
    validator = ComplianceValidator()
    return validator.validate(
        rules=kwargs["rules"],
        employees=context["employees"],
        assignments=shifts,
        shift_templates=context["templates"],
        period_start=kwargs["period_start"],
        period_end=kwargs["period_end"],
        weeks_in_period=kwargs["weeks_in_period"],
        employee_target_hours=context["target_hours"],
    )


def _availability_status_breakdown(payload: dict) -> Counter:
    return Counter(row["status"] for row in payload["availability"])


def main() -> None:
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)  # 8-week horizon
    weeks_in_period = 8

    print("=" * 72)
    print("ROUTER-8H SHADOW TEST")
    print("=" * 72)

    payload, context = export_period(
        period_start=period_start,
        period_end=period_end,
        weeks_in_period=weeks_in_period,
    )
    status = _availability_status_breakdown(payload)
    blocked_cells = sum(len(v) for v in context["blocks"].values())
    print(f"Period           : {period_start} -> {period_end} ({payload['period']['days']} days)")
    print(f"Employees        : {len(payload['employees'])}")
    print(f"Shift types      : {[s['code'] for s in payload['shift_types']]}")
    print(f"Availability cells: {sum(status.values())} "
          f"(available={status.get('available', 0)}, blocked={status.get('blocked', 0)})")
    print(f"Injected PTO/sick: {blocked_cells} blocked cells across {len(context['blocks'])} staff")
    print(f"Demand rows      : {len(payload['demand'])}")
    print(f"Constraint records: {[c['kind'] for c in payload['constraints']]}")

    print("-" * 72)
    print("Stage 2: routing (STAND-IN = legacy engine; swap for live LLM API)")
    routed = reference_route(payload, context)
    n_assignments = sum(len(v) for v in routed.values())
    print(f"Router produced  : {n_assignments} assignments across {len(routed)} employees")

    print("-" * 72)
    print("Stage 3: receiver + Demand-minus-Assigned delta")
    shifts = ingest_router_response(routed, context)
    total_gap, gaps = compute_gap_delta(routed, context)
    print(f"Parsed shifts    : {len(shifts)}")
    print(f"Unfilled gaps    : {total_gap} (Demand - Assigned, computed by Python receiver)")
    for gap in gaps[:5]:
        print(f"    GAP {gap['date']} {gap['shift_code']}: need {gap['required']}, have {gap['assigned']}")
    if len(gaps) > 5:
        print(f"    ... +{len(gaps) - 5} more gap rows")

    print("-" * 72)
    print("Stage 4: umpire (legacy ComplianceValidator.validate)")
    result = run_umpire(shifts, context)
    print(f"Passed           : {result.passed}")
    print(f"Pass rate        : {result.pass_rate_pct:.1f}%")
    print(f"Conflicts        : {result.conflict_count}")
    print(f"Warnings         : {len(result.warnings)}")
    if result.manager_summary:
        print("Top conflict types:")
        for label in result.manager_summary[:8]:
            print(f"    - {label}")

    print("-" * 72)
    print("Stage 5: financial score + gainshare delta (penalty_score.py)")
    score_kwargs = dict(
        employees=context["employees"],
        target_hours=context["target_hours"],
        shift_templates=context["templates"],
        daily_demand=context["daily_demand"],
    )
    baseline_routed = baseline_route(payload, context)
    baseline_score: PenaltyBreakdown = score_schedule(assignments=baseline_routed, **score_kwargs)
    agent_score: PenaltyBreakdown = score_schedule(assignments=routed, **score_kwargs)
    delta = gainshare_delta(baseline_score, agent_score)

    def _print_score(label: str, s: PenaltyBreakdown) -> None:
        print(f"  {label}: total=${s.total_penalty:,.0f} "
              f"(FTE-OT=${s.fte_overage_penalty:,.0f} [{s.overage_hours:.0f}h], "
              f"gaps=${s.unfilled_gap_penalty:,.0f} [{s.unfilled_gap_count}], "
              f"weekend=${s.weekend_asymmetry_penalty:,.0f})")

    _print_score("Baseline (concentrated/OT)", baseline_score)
    _print_score("Agent    (router output)  ", agent_score)
    print(f"  GAINSHARE: ${delta['saved']:,.0f} saved vs baseline ({delta['saved_pct']:.1f}%)")

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / "flat_payload.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (ARTIFACT_DIR / "router_output.json").write_text(json.dumps(routed, indent=2), encoding="utf-8")
    (ARTIFACT_DIR / "compliance_report.json").write_text(
        json.dumps(
            {
                "passed": result.passed,
                "pass_rate_pct": result.pass_rate_pct,
                "conflict_count": result.conflict_count,
                "warning_count": len(result.warnings),
                "unfilled_gap_total": total_gap,
                "unfilled_gaps": gaps,
                "conflict_labels": result.manager_summary,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (ARTIFACT_DIR / "financial_report.json").write_text(
        json.dumps(
            {
                "weights": {"fte_overage_per_hour": 85.0, "unfilled_gap_per_hour": 150.0, "weekend_variance_per_unit": 25.0},
                "baseline": baseline_score.to_dict(),
                "agent": agent_score.to_dict(),
                "gainshare": delta,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (ARTIFACT_DIR / "router_8h_prompt.txt").write_text(ROUTER_8H_SYSTEM_PROMPT, encoding="utf-8")
    print("-" * 72)
    print(f"Artifacts written to: {ARTIFACT_DIR}")
    print("=" * 72)


if __name__ == "__main__":
    main()
