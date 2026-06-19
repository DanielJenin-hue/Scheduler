"""Tests for the Sidecar deterministic 7-on/7-off stamper (4-line pod)."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta

import pytest

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.compliance.engine import ShiftTemplateInfo
from lab_scheduler.scheduling.auto_pilot import AutoPilotError, AutoPilotRunResult
from lab_scheduler.scheduling.deterministic_stamper import (
    POD_CYCLE_DAYS,
    POD_SIZE,
    deterministic_stamper,
    pod_stagger_offset_days,
)
from lab_scheduler.scheduling.profiles import EmployeeProfile
from lab_scheduler.scheduling.strategies import ScheduleArchetype
from lab_scheduler.scheduling.strategies.twelve_hour_7on7off_strategy import (
    FTE_TOPUP_TEMPLATE_ID,
)

from portage_fixtures import portage_generate_kwargs

pytestmark = pytest.mark.legacy

_STAMPER_PARAMS = (
    "rules",
    "period_start",
    "period_end",
    "weeks_in_period",
    "employees",
    "shift_templates",
    "shift_required_qualifications",
    "employee_target_hours",
    "availability_blocked",
)


def _kwargs():
    raw = portage_generate_kwargs(
        period_start=date(2026, 6, 1),
        period_end=date(2026, 7, 26),
        weeks_in_period=8,
    )
    # The sidecar exposes only the deterministic inputs; drop solver-only kwargs.
    return {key: raw[key] for key in _STAMPER_PARAMS if key in raw}


def _code_by_id(kwargs):
    return {tid: t.code.upper() for tid, t in kwargs["shift_templates"].items()}


def _daily_token_counts(result, kwargs, token_code):
    code_by_id = _code_by_id(kwargs)
    counts: Counter[date] = Counter()
    for assignment in result.generate.assignments:
        if assignment.shift_template_id == FTE_TOPUP_TEMPLATE_ID:
            continue
        if code_by_id.get(assignment.shift_template_id) == token_code:
            counts[assignment.assignment_date] += 1
    return counts


# --------------------------------------------------------------------------- #
# Pod math
# --------------------------------------------------------------------------- #


def test_pod_constants():
    assert POD_SIZE == 4
    assert POD_CYCLE_DAYS == 28


def test_pod_stagger_offset_uses_modulo_four_times_seven():
    assert pod_stagger_offset_days(0) == 0
    assert pod_stagger_offset_days(1) == 7
    assert pod_stagger_offset_days(2) == 14
    assert pod_stagger_offset_days(3) == 21
    # Pod wraps every four lines.
    assert pod_stagger_offset_days(4) == 0
    assert pod_stagger_offset_days(5) == 7


# --------------------------------------------------------------------------- #
# Result shape
# --------------------------------------------------------------------------- #


def test_stamper_returns_finalized_twelve_hour_result():
    result = deterministic_stamper(**_kwargs())
    assert isinstance(result, AutoPilotRunResult)
    assert result.generate.schedule_archetype == ScheduleArchetype.TWELVE_HOUR.value
    assert result.generate.schedule_status == "FINAL"
    assert result.generate.assignments
    assert result.proof.compliance_error_count == 0


def test_stamper_full_time_lines_are_contiguous_seven_on_seven_off():
    result = deterministic_stamper(**_kwargs())
    kwargs = _kwargs()
    period_start = date(2026, 6, 1)
    for employee in kwargs["employees"]:
        if employee.fte < 0.99:
            continue
        scheduled = {
            assignment.assignment_date
            for assignment in result.generate.assignments
            if assignment.employee_id == employee.id
        }
        pattern = "".join(
            "X" if period_start + timedelta(days=offset) in scheduled else "."
            for offset in range(56)
        )
        assert "X.X" not in pattern
        topups = [
            assignment
            for assignment in result.generate.assignments
            if assignment.employee_id == employee.id
            and assignment.shift_template_id == FTE_TOPUP_TEMPLATE_ID
        ]
        assert len(topups) == 1


# --------------------------------------------------------------------------- #
# FTE top-up placement: boundary-only (Day 7 of block / Day 1 of next block)
# --------------------------------------------------------------------------- #


def _contiguous_runs(days):
    runs = []
    current = []
    for day in sorted(days):
        if current and (day - current[-1]).days == 1:
            current.append(day)
        else:
            if current:
                runs.append(current)
            current = [day]
    if current:
        runs.append(current)
    return runs


def test_topup_lands_only_on_block_boundary_days():
    """Each full-time 'T' must sit at the first or last day of its on-block, never interior."""

    kwargs = _kwargs()
    result = deterministic_stamper(**kwargs)
    full_time_ids = {e.id for e in kwargs["employees"] if e.fte >= 0.99}

    checked = 0
    for employee_id in full_time_ids:
        worked = {
            a.assignment_date
            for a in result.generate.assignments
            if a.employee_id == employee_id
            and a.shift_template_id != FTE_TOPUP_TEMPLATE_ID
        }
        topups = [
            a.assignment_date
            for a in result.generate.assignments
            if a.employee_id == employee_id
            and a.shift_template_id == FTE_TOPUP_TEMPLATE_ID
        ]
        assert len(topups) == 1
        topup_day = topups[0]

        # Reconstruct the on-block (worked days + the topup day it replaced) and assert
        # the topup sits at an endpoint of that contiguous run.
        block_days = worked | {topup_day}
        run = next(r for r in _contiguous_runs(block_days) if topup_day in r)
        assert topup_day == run[0] or topup_day == run[-1]
        checked += 1

    assert checked > 0


# --------------------------------------------------------------------------- #
# Coverage balance (the headline fix)
# --------------------------------------------------------------------------- #


def test_stamper_night_coverage_is_flat_and_never_zero():
    kwargs = _kwargs()
    result = deterministic_stamper(**kwargs)
    night_counts = _daily_token_counts(result, kwargs, "NIGHT")
    dates = [date(2026, 6, 1) + timedelta(days=offset) for offset in range(56)]
    nightly = [night_counts[day] for day in dates]
    assert min(nightly) > 0
    # Day/Night pods spread 4 consecutive D/N lines across all 4 phases -> flat row.
    assert len(set(nightly)) == 1


def test_stamper_day_coverage_never_collapses_to_zero():
    kwargs = _kwargs()
    result = deterministic_stamper(**kwargs)
    day_counts = _daily_token_counts(result, kwargs, "MORNING")
    dates = [date(2026, 6, 1) + timedelta(days=offset) for offset in range(56)]
    daily = [day_counts[day] for day in dates]
    assert min(daily) > 0


def test_stamper_coverage_guard_aborts_when_night_collapses():
    """A lone D/N full-time line has off-weeks with zero Night coverage -> abort."""

    templates = {
        "shift-morning": ShiftTemplateInfo(
            id="shift-morning",
            code="MORNING",
            name="Morning",
            start_time="07:00",
            end_time="19:00",
            duration_minutes=720,
            crosses_midnight=False,
        ),
        "shift-night": ShiftTemplateInfo(
            id="shift-night",
            code="NIGHT",
            name="Night",
            start_time="19:00",
            end_time="07:00",
            duration_minutes=720,
            crosses_midnight=True,
        ),
    }
    lone_line = [
        EmployeeProfile(
            id="line-01",
            full_name="Vacant MLA D/N - Line 01",
            fte=1.0,
            qualification_ids={"qual-mla"},
            seniority_hours=1000.0,
            base_hourly_rate=30.0,
            contract_line_type="D/N",
        )
    ]
    with pytest.raises(AutoPilotError):
        deterministic_stamper(
            rules=MANITOBA,
            period_start=date(2026, 6, 1),
            period_end=date(2026, 7, 26),
            weeks_in_period=8,
            employees=lone_line,
            shift_templates=templates,
            shift_required_qualifications={},
        )


# --------------------------------------------------------------------------- #
# Guards / isolation
# --------------------------------------------------------------------------- #


def test_stamper_rejects_non_monday_start():
    kwargs = _kwargs()
    kwargs["period_start"] = date(2026, 6, 2)  # Tuesday
    with pytest.raises(AutoPilotError):
        deterministic_stamper(**kwargs)


def test_stamper_does_not_import_gamified_solver_modules():
    """The sidecar must not pull in the CP-SAT / standard-solver pathway."""

    import sys

    for solver_module in (
        "lab_scheduler.solver.cpsat_fill",
        "lab_scheduler.scheduling.strategies.standard_strategy",
    ):
        sys.modules.pop(solver_module, None)

    import importlib

    importlib.reload(importlib.import_module("lab_scheduler.scheduling.deterministic_stamper"))
    deterministic_stamper(**_kwargs())

    assert "lab_scheduler.solver.cpsat_fill" not in sys.modules
    assert "lab_scheduler.scheduling.strategies.standard_strategy" not in sys.modules
