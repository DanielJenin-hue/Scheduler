"""Anchor & Fill architecture tests."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.legacy

from datetime import date

from lab_scheduler.compliance import MANITOBA
from lab_scheduler.scheduling.anchor_tiers import (
    AnchorTier,
    anchor_tier_for_cell,
    is_night_anchor_cell,
    merge_night_anchor_fixed_bands,
)
from lab_scheduler.scheduling.balance_advisor import suggest_swaps_for_tally_variance
from lab_scheduler.scheduling.auto_generate import EmployeeProfile, PlannedAssignment
from lab_scheduler.scheduling.equitability_score import FairnessWeights, score_line
from lab_scheduler.simulation.hospital_stress import shift_templates as stress_templates


def _dn_line(line: str) -> EmployeeProfile:
    return EmployeeProfile(
        f"dn-{line}",
        f"Vacant MLT D/N - Line {line}",
        1.0,
        {"qual-mlt"},
        contract_line_type="D/N",
    )


def test_night_anchor_tier_on_catalog_n_cells() -> None:
    employee = _dn_line("01")
    period_start = date(2026, 6, 1)
    night_date = date(2026, 6, 1)  # catalog N block week 0 line 01
    tier = anchor_tier_for_cell(employee, night_date, period_start)
    assert tier == AnchorTier.NIGHT_ANCHOR
    assert is_night_anchor_cell(employee, night_date, period_start)


def test_equitability_score_prefers_hour_deficit() -> None:
    low_hours = EmployeeProfile("a", "Vacant MLT D/E - Line 01", 1.0, {"qual-mlt"})
    high_hours = EmployeeProfile("b", "Vacant MLT D/E - Line 02", 1.0, {"qual-mlt"})
    weights = FairnessWeights(hour_deficit=2.0)
    period_start = date(2026, 6, 1)
    period_end = date(2026, 7, 26)
    low_score = score_line(
        low_hours,
        total_hours=120.0,
        target_hours=320.0,
        work_dates=set(),
        assignments=[],
        shift_templates=stress_templates(),
        period_start=period_start,
        period_end=period_end,
        weights=weights,
    )
    high_score = score_line(
        high_hours,
        total_hours=280.0,
        target_hours=320.0,
        work_dates=set(),
        assignments=[],
        shift_templates=stress_templates(),
        period_start=period_start,
        period_end=period_end,
        weights=weights,
    )
    assert low_score < high_score


def test_balance_advisor_never_proposes_n_band() -> None:
    assert (
        suggest_swaps_for_tally_variance(
            band="N",
            assignment_date=date(2026, 6, 3),
            assignments=[],
            employees=[_dn_line("01")],
            shift_templates=stress_templates(),
            period_start=date(2026, 6, 1),
        )
        == []
    )


def test_balance_advisor_skips_night_anchors() -> None:
    shift_templates = stress_templates()
    night_id = next(tid for tid, info in shift_templates.items() if info.code == "NIGHT")
    employee = _dn_line("01")
    period_start = date(2026, 6, 1)
    night_date = date(2026, 6, 1)
    assignments = [
        PlannedAssignment(
            employee_id=employee.id,
            shift_template_id=night_id,
            assignment_date=night_date,
        )
    ]
    options = suggest_swaps_for_tally_variance(
        band="N",
        assignment_date=night_date,
        assignments=assignments,
        employees=[employee],
        shift_templates=shift_templates,
        period_start=period_start,
    )
    assert options == []


def test_merge_night_anchor_fixed_bands_for_cpsat() -> None:
    employee = _dn_line("01")
    period_start = date(2026, 6, 1)
    period_end = date(2026, 6, 14)
    merged = merge_night_anchor_fixed_bands(
        {},
        employees=[employee],
        period_start=period_start,
        period_end=period_end,
    )
    assert any(band == "N" for band in merged.values())
    assert all(key[0] == employee.id for key in merged)


def test_remove_assignment_guarded_blocks_night_anchor() -> None:
    from lab_scheduler.scheduling.auto_generate import (
        PlannedAssignment,
        _EmployeeState,
        _remove_assignment_at_index_guarded,
    )
    from lab_scheduler.scheduling.post_pass_guard import PostPassGuard

    shift_templates = stress_templates()
    night_id = next(tid for tid, info in shift_templates.items() if info.code == "NIGHT")
    employee = _dn_line("01")
    period_start = date(2026, 6, 1)
    anchor_date = date(2026, 6, 1)
    assignments = [
        PlannedAssignment(
            employee_id=employee.id,
            shift_template_id=night_id,
            assignment_date=anchor_date,
        )
    ]
    states = {employee.id: _EmployeeState(profile=employee, target_hours=320.0)}
    violations: list[str] = []
    guard = PostPassGuard(
        frozen_master_cells=set(),
        employees=(employee,),
        period_start=period_start,
    )
    removed = _remove_assignment_at_index_guarded(
        assignments,
        0,
        states=states,
        shift_templates=shift_templates,
        post_pass_guard=guard,
        anchor_violations=violations,
    )
    assert removed is False
    assert len(assignments) == 1
    assert violations
