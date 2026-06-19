"""Tests for the ROUTER-8H financial penalty scorer and gainshare delta."""

from __future__ import annotations

from datetime import date

from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.finance.penalty_score import (
    DEFAULT_WEIGHTS,
    PenaltyWeights,
    gainshare_delta,
    score_schedule,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile

TEMPLATES = {
    "day": ShiftTemplateInfo("day", "MORNING", "Day", "07:00", "15:00", 480, False),
    "eve": ShiftTemplateInfo("eve", "EVENING", "Eve", "15:00", "23:00", 480, False),
    "night": ShiftTemplateInfo("night", "NIGHT", "Night", "23:00", "07:00", 480, True),
}


def _emp(emp_id: str, qual: str = "qual-mlt") -> EmployeeProfile:
    return EmployeeProfile(id=emp_id, full_name=emp_id, fte=1.0, qualification_ids={qual})


def test_fte_overage_penalty():
    # One employee works 8h over a tiny target; weight = 85/hr.
    emp = _emp("e1")
    # Two MORNING shifts on weekdays (Mon/Tue) = 16h, target 8h -> 8h overage.
    routed = {"e1": {"2026-06-01": "MORNING", "2026-06-02": "MORNING"}}
    breakdown = score_schedule(
        employees=[emp],
        target_hours={"e1": 8.0},
        assignments=routed,
        shift_templates=TEMPLATES,
        daily_demand={},  # no demand -> no gap penalty
    )
    assert breakdown.overage_hours == 8.0
    assert breakdown.fte_overage_penalty == 8.0 * 85.0  # 680
    assert breakdown.unfilled_gap_penalty == 0.0
    assert breakdown.total_penalty == 680.0


def test_unfilled_gap_penalty_is_1200_per_8h_shift():
    emp = _emp("e1")
    # Demand for 1 MORNING seat on a weekday, nobody assigned -> 1 gap of 8h.
    breakdown = score_schedule(
        employees=[emp],
        target_hours={"e1": 999.0},
        assignments={},
        shift_templates=TEMPLATES,
        daily_demand={date(2026, 6, 1): {"MORNING": 1}},
    )
    assert breakdown.unfilled_gap_count == 1
    assert breakdown.unfilled_gap_hours == 8.0
    assert breakdown.unfilled_gap_penalty == 8.0 * 150.0  # 1200


def test_gap_penalty_exceeds_overtime_penalty_for_same_hour():
    # Strategy invariant: filling a gap via OT (85/hr) must beat leaving it (150/hr).
    assert DEFAULT_WEIGHTS.unfilled_gap_per_hour > DEFAULT_WEIGHTS.fte_overage_per_hour

    emp = _emp("e1")
    day = date(2026, 6, 1)  # weekday
    demand = {day: {"MORNING": 1}}

    leave_gap = score_schedule(
        employees=[emp], target_hours={"e1": 0.0},
        assignments={}, shift_templates=TEMPLATES, daily_demand=demand,
    )
    fill_with_ot = score_schedule(
        employees=[emp], target_hours={"e1": 0.0},  # every hour is overage
        assignments={"e1": {day.isoformat(): "MORNING"}},
        shift_templates=TEMPLATES, daily_demand=demand,
    )
    # Filling the 8h gap with OT costs 8*85=680; leaving it costs 8*150=1200.
    assert fill_with_ot.total_penalty == 680.0
    assert leave_gap.total_penalty == 1200.0
    assert fill_with_ot.total_penalty < leave_gap.total_penalty


def test_weekend_asymmetry_penalty():
    # 2026-06-06 and 2026-06-07 are Sat/Sun. e1 works both weekend days; e2 none.
    employees = [_emp("e1"), _emp("e2")]
    routed = {
        "e1": {"2026-06-06": "MORNING", "2026-06-07": "MORNING"},
    }
    breakdown = score_schedule(
        employees=employees,
        target_hours={"e1": 999.0, "e2": 999.0},
        assignments=routed,
        shift_templates=TEMPLATES,
        daily_demand={},
    )
    # Counts: e1=2, e2=0 -> mean 1.0 -> floor avg 1. Excess = (2-1) + 0 = 1 unit.
    assert breakdown.weekend_floor_average == 1
    assert breakdown.weekend_excess_shifts == 1
    assert breakdown.weekend_asymmetry_penalty == 25.0


def test_accepts_scheduled_shift_records():
    from lab_scheduler.compliance.engine import ScheduledShift

    emp = _emp("e1")
    shifts = [
        ScheduledShift(employee_id="e1", employee_name="e1", assignment_date=date(2026, 6, 1), shift_template_id="day"),
        ScheduledShift(employee_id="e1", employee_name="e1", assignment_date=date(2026, 6, 2), shift_template_id="day"),
    ]
    breakdown = score_schedule(
        employees=[emp], target_hours={"e1": 8.0},
        assignments=shifts, shift_templates=TEMPLATES, daily_demand={},
    )
    assert breakdown.fte_overage_penalty == 8.0 * 85.0


def test_gainshare_delta_positive_when_agent_cheaper():
    emp = _emp("e1")
    common = dict(
        employees=[emp], target_hours={"e1": 8.0},
        shift_templates=TEMPLATES, daily_demand={},
    )
    baseline = score_schedule(
        assignments={"e1": {"2026-06-01": "MORNING", "2026-06-02": "MORNING", "2026-06-03": "MORNING"}},
        **common,
    )  # 24h, 16h overage -> 1360
    agent = score_schedule(
        assignments={"e1": {"2026-06-01": "MORNING", "2026-06-02": "MORNING"}},
        **common,
    )  # 16h, 8h overage -> 680
    delta = gainshare_delta(baseline, agent)
    assert delta["baseline_total"] == 1360.0
    assert delta["agent_total"] == 680.0
    assert delta["saved"] == 680.0
    assert delta["saved_pct"] == 50.0


def test_weight_override_recomputes_total():
    emp = _emp("e1")
    routed = {"e1": {"2026-06-01": "MORNING", "2026-06-02": "MORNING"}}
    common = dict(
        employees=[emp], target_hours={"e1": 8.0},
        assignments=routed, shift_templates=TEMPLATES, daily_demand={},
    )
    default = score_schedule(**common)
    codebase_rate = score_schedule(**common, weights=PenaltyWeights(fte_overage_per_hour=60.0))
    assert default.fte_overage_penalty == 8.0 * 85.0
    assert codebase_rate.fte_overage_penalty == 8.0 * 60.0


def test_determinism():
    emp = _emp("e1")
    routed = {"e1": {"2026-06-01": "MORNING", "2026-06-06": "NIGHT"}}
    common = dict(
        employees=[emp], target_hours={"e1": 100.0},
        assignments=routed, shift_templates=TEMPLATES,
        daily_demand={date(2026, 6, 1): {"NIGHT": 1}},
    )
    assert score_schedule(**common) == score_schedule(**common)
